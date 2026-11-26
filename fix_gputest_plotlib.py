#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gputest 出图补丁：修掉 conda 环境下 glib-2.0 被解析成目录导致的绘图失败。

问题
    summary_monitor/plot_metrics.py 不用 matplotlib，而是用 ctypes 直接绑定系统
    librsvg/Cairo 把 SVG 栅格化成 PNG：

        for name in ('rsvg-2', 'cairo', 'gobject-2.0', 'glib-2.0'):
            filename = ctypes.util.find_library(name)
            libraries[name] = ctypes.CDLL(filename)

    在 conda 环境里（实测 torch110-cu113 / Python 3.7.15），
    find_library('glib-2.0') 返回的是

        /mnt/user/test/miniconda3/envs/torch110-cu113/lib/glib-2.0

    而这在 conda 环境里是一个**目录**（里面只有 include/，放 glib 的 gdb 脚本），
    并不是共享库。CDLL 打开目录就抛：

        OSError: .../lib/glib-2.0: cannot read file data: Is a directory

    现象就是 collect_metrics.py 收尾时 render() 建好了 plots/ 目录，却一张 PNG
    都没写出来，run_direct.py 报“监控不完整或绘图失败”。

    （用系统自带的 /usr/bin/python3 跑同一份 metrics.csv 完全正常，
    因为它的 find_library 返回的是裸 soname 'libglib-2.0.so.0'。）

做法
    只改 _png_backend() 里的库定位逻辑，不碰任何绘图/统计代码：

      - 仍然优先用 find_library 的结果，但只接受“真实存在的普通文件”，
        或者不带目录成分的裸 soname；
      - 否则按 lib<name>.so.0 / .so.2 / .so / <name> 依次回退，
        让动态链接器自己按 ld.so.cache 搜索（Cairo/librsvg 都是系统级的）；
      - 全部失败时才抛原来的 RuntimeError，并在末尾附上试过的候选名。

    纯标准库，兼容 Python 3.6+，不新增依赖，不打补丁时行为与现在完全一致。

用法
    python3 fix_gputest_plotlib.py                  # 只打印将产生的 diff（默认）
    python3 fix_gputest_plotlib.py --write          # 落盘（自动 .bak 备份 + 语法校验）
    python3 fix_gputest_plotlib.py --write --test   # 落盘后自检：真出一个小 PNG
    python3 fix_gputest_plotlib.py --check          # 只看状态，不生成 diff
    python3 fix_gputest_plotlib.py --save-patch glib-fix.patch   # 另存可 git apply 的补丁
    python3 fix_gputest_plotlib.py --root /path/to/AUTOPERF-GPU  # 显式指定仓库根目录

    默认从当前目录向上找 summary_monitor/plot_metrics.py，找不到用 --root。
    重复执行是幂等的：打过的补丁只会提示“已打过补丁”，不会重复插入。
"""

from __future__ import print_function

import argparse
import difflib
import importlib.util
import os
import shutil
import sys
import tempfile
import time

VERSION = '1.0.0'
TARGET_REL = os.path.join('summary_monitor', 'plot_metrics.py')
MARKER = '_load_library'
BACKUP_SUFFIX = '.bak'

HELPERS = '''def _library_candidates(name):
    """Ordered dlopen candidates for a system library, best first.

    ``ctypes.util.find_library`` is not trustworthy on every interpreter: conda
    environments ship a ``lib/<name>`` *directory* (``lib/glib-2.0`` only holds
    headers) and some pythons hand that path back, which dlopen rejects with
    "cannot read file data: Is a directory". Only a real file, or a bare soname
    without a directory part, is accepted; otherwise the plain sonames below let
    the dynamic loader search its own cache.
    """
    import ctypes.util
    import os
    found = ctypes.util.find_library(name)
    usable = bool(found) and (os.path.isfile(found) or os.path.dirname(found) == '')
    candidates = [found] if usable else []
    for suffix in ('.so.0', '.so.2', '.so', ''):
        candidate = 'lib' + name + suffix
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _load_library(name):
    """Return a CDLL for a system library, or raise the documented error."""
    import ctypes as c
    attempts = []
    for candidate in _library_candidates(name):
        try:
            return c.CDLL(candidate)
        except OSError as exc:
            attempts.append('{} ({})'.format(candidate, exc))
    raise RuntimeError('PNG output requires system librsvg/Cairo; missing ' + name
                       + ' (install librsvg2/cairo; this tool only writes PNG charts)'
                       + '; tried ' + ', '.join(attempts))


'''

OLD_LOOP = """    if hasattr(_png_backend, 'cached'):
        return _png_backend.cached
    libraries = {}
    for name in ('rsvg-2', 'cairo', 'gobject-2.0', 'glib-2.0'):
        libraries[name] = _load_library(name)
"""

# 从 ``import ctypes.util`` 起替换：补丁后这行没人用了，顺手清掉，避免 pyflakes F401。
START_ANCHOR = '    import ctypes.util\n'
END_ANCHOR = 'libraries[name] = c.CDLL(filename)\n'
FUNC_ANCHOR = 'def _png_backend():'


class PatchError(Exception):
    pass


_KEEP_STREAMS = []


def _force_utf8_stdio():
    """POSIX/C 环境下 sys.stdout 默认是 ascii，中文会直接抛 UnicodeEncodeError。"""
    import io
    for name in ('stdout', 'stderr'):
        stream = getattr(sys, name, None)
        buffer = getattr(stream, 'buffer', None)
        if buffer is None:
            continue
        encoding = (getattr(stream, 'encoding', '') or '').lower().replace('-', '')
        if encoding == 'utf8':
            continue
        try:
            wrapper = io.TextIOWrapper(buffer, encoding='utf-8', errors='replace',
                                       line_buffering=True, write_through=True)
        except Exception:  # noqa: BLE001 - 拿不到就退回原 stream
            continue
        setattr(sys, name, wrapper)
        _KEEP_STREAMS.append((stream, wrapper))


def locate_root(explicit=None):
    """Return the gputest root that contains summary_monitor/plot_metrics.py."""
    bases = []
    if explicit:
        bases.append(os.path.abspath(explicit))
    bases.append(os.getcwd())
    bases.append(os.path.dirname(os.path.abspath(__file__)))
    for base in bases:
        current = base
        while True:
            if os.path.isfile(os.path.join(current, TARGET_REL)):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
    return None


def state_of(text):
    """'patched' | 'patchable' | 'unknown' (anchors not found)."""
    if MARKER in text:
        return 'patched'
    if START_ANCHOR in text and END_ANCHOR in text and FUNC_ANCHOR in text:
        return 'patchable'
    return 'unknown'


def patched_text(text):
    """Return (new_text, notes). Raises PatchError when anchors are unusable."""
    state = state_of(text)
    if state == 'patched':
        raise PatchError('already patched')
    if state != 'patchable':
        raise PatchError('锚点没找到：文件结构与预期不同（可能是别的版本），拒绝盲目修改')

    start = text.index(START_ANCHOR)
    end = text.index(END_ANCHOR, start) + len(END_ANCHOR)
    region = text[start:end]
    for needle in ('find_library', 'rsvg-2', 'glib-2.0'):
        if needle not in region:
            raise PatchError('待替换区间里没出现 {!r}，不敢动'.format(needle))

    notes = []
    updated = text[:start] + OLD_LOOP + text[end:]
    notes.append('_png_backend(): 去掉不再使用的 import ctypes.util，库定位改用 _load_library(name) 逐个回退')

    anchor = updated.index(FUNC_ANCHOR)
    # 保持两份空行（PEP8 顶层函数间隔）
    prefix = updated[:anchor]
    if not prefix.endswith('\n\n'):
        prefix = prefix.rstrip('\n') + '\n\n\n'
    updated = prefix + HELPERS + updated[anchor:]
    notes.append('新增 _library_candidates() / _load_library() 两个 helper')
    return updated, notes


def unified_diff(path, root, before, after):
    """Unified diff labelled so that `git apply -p1` works from the repo root."""
    rel = os.path.relpath(path, root).replace(os.sep, '/')
    return ''.join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True),
        fromfile='a/' + rel, tofile='b/' + rel, n=4))


def try_compile(path):
    """Syntax-check the file in memory; return None on success, error text otherwise."""
    try:
        with open(path, 'r', encoding='utf-8') as stream:
            source = stream.read()
        compile(source, path, 'exec')
    except (SyntaxError, ValueError) as exc:
        return '{}: {}'.format(type(exc).__name__, exc)
    except OSError as exc:
        return str(exc)
    return None


def self_test(path):
    """Import the patched module and rasterize a tiny SVG to prove PNG output works."""
    print('\n自检：用当前解释器 {} 加载补丁后的模块'.format(sys.executable))
    spec = importlib.util.spec_from_file_location('plot_metrics_patched', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules['plot_metrics_patched'] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - 自检要把失败原因原样打出来
        print('❌ 模块导入失败: {}: {}'.format(type(exc).__name__, exc))
        return 1

    import ctypes as c
    failures = 0
    for name in ('rsvg-2', 'cairo', 'gobject-2.0', 'glib-2.0'):
        candidates = module._library_candidates(name)
        chosen = None
        for candidate in candidates:
            try:
                c.CDLL(candidate)
            except OSError:
                continue
            chosen = candidate
            break
        if chosen:
            print('  ✅ {:<13} → {}   (候选: {})'.format(name, chosen, ', '.join(candidates)))
        else:
            failures += 1
            print('  ❌ {:<13} 全部候选都加载失败: {}'.format(name, ', '.join(candidates)))
    if failures:
        return 1

    try:
        module._png_backend()
        print('  ✅ _png_backend() 四库全部绑定成功')
    except Exception as exc:  # noqa: BLE001
        print('❌ _png_backend() 仍然失败: {}: {}'.format(type(exc).__name__, exc))
        return 1

    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="40" height="20">'
           '<rect width="40" height="20" fill="#2563eb"/></svg>')
    target = os.path.join(tempfile.mkdtemp(prefix='gputest-plot-selftest-'), 'selftest.png')
    try:
        module._write_png(svg, target, height=20, scale=1)
    except Exception as exc:  # noqa: BLE001
        print('❌ 试画 PNG 失败: {}: {}'.format(type(exc).__name__, exc))
        return 1
    size = os.path.getsize(target) if os.path.isfile(target) else 0
    if not size:
        print('❌ 试画 PNG 没有产出文件')
        return 1
    print('  ✅ 试画 PNG 成功: {} ({} 字节)'.format(target, size))
    return 0


def main(argv=None):
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        description='给 gputest 的 summary_monitor/plot_metrics.py 打“库定位”补丁，'
                    '修掉 conda 环境下 glib-2.0 被解析成目录导致的绘图失败。',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='默认只打印 diff，不写文件；加 --write 才落盘（幂等）。')
    parser.add_argument('-r', '--root', help='gputest 仓库根目录（默认从当前目录向上查找）')
    parser.add_argument('-w', '--write', action='store_true', help='真正写入（先备份 .bak）')
    parser.add_argument('-c', '--check', action='store_true', help='只看状态，不生成 diff')
    parser.add_argument('-t', '--test', action='store_true', help='打补丁后自检：试画一个 PNG')
    parser.add_argument('-p', '--save-patch', metavar='FILE', help='把 diff 另存为可 git apply 的补丁文件')
    parser.add_argument('-V', '--version', action='version', version='%(prog)s ' + VERSION)
    args = parser.parse_args(argv)

    root = locate_root(args.root)
    if not root:
        print('❌ 找不到 summary_monitor/plot_metrics.py；用 --root 指定 gputest 仓库根目录', file=sys.stderr)
        return 1
    path = os.path.join(root, TARGET_REL)
    with open(path, 'r', encoding='utf-8') as stream:
        before = stream.read()

    state = state_of(before)
    print('gputest 根目录: {}'.format(root))
    print('目标文件      : {}'.format(path))
    print('Python        : {} {}'.format(sys.version.split()[0], sys.executable))

    if state == 'patched':
        print('状态          : 已打过补丁（检测到 {}），无需再改'.format(MARKER))
        if args.test:
            return self_test(path)
        return 0
    if state == 'unknown':
        print('状态          : ❌ 未识别（找不到预期锚点，可能是不同版本）', file=sys.stderr)
        return 1

    print('状态          : 未打补丁，可以升级')
    try:
        after, notes = patched_text(before)
    except PatchError as exc:
        print('❌ {}'.format(exc), file=sys.stderr)
        return 1

    print('\n将做以下修改:')
    for index, note in enumerate(notes, 1):
        print('  {}. {}'.format(index, note))

    diff = unified_diff(path, root, before, after)
    if args.save_patch:
        with open(args.save_patch, 'w', encoding='utf-8') as stream:
            stream.write(diff)
        print('\n补丁已另存: {}（在仓库根目录执行 git apply -p1 {}）'.format(args.save_patch, args.save_patch))

    if args.check:
        return 0

    if not args.write:
        print('\n' + ('-' * 72))
        print(diff, end='')
        print('-' * 72)
        print('\n这是 dry-run，没有写任何文件。确认无误后加 --write 落盘。')
        return 0

    backup = '{}{}.{}'.format(path, BACKUP_SUFFIX, time.strftime('%Y%m%d_%H%M%S'))
    shutil.copy2(path, backup)
    with open(path, 'w', encoding='utf-8') as stream:
        stream.write(after)
    error = try_compile(path)
    if error:
        shutil.copy2(backup, path)
        print('❌ 打补丁后语法校验失败，已回滚: {}'.format(error), file=sys.stderr)
        return 1
    print('\n✅ 已写入（备份: {}），语法校验通过'.format(backup))
    if args.test:
        return self_test(path)
    print('接着跑：python3 summary_monitor/plot_metrics.py <metrics.csv> --output-dir <dir>')
    return 0


if __name__ == '__main__':
    sys.exit(main())

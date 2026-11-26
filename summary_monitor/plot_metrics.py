"""Metric PNG reports (system librsvg/Cairo rasterizes the generated chart SVG).

Run: python3 summary_monitor/plot_metrics.py samples.csv --output-dir report
Missing/non-finite measurements break lines. Peaks always use the original CSV;
only drawing is reduced to first/min/max/last samples in each horizontal pixel.
"""

import argparse
import csv
import html
import json
import math
from pathlib import Path


# column, output stem, display label, unit, color
METRICS = (
    ("cpu_pct", "cpu", "CPU", "%", "#2563eb"),
    ("vram_mib", "vram", "VRAM", "MiB", "#7c3aed"),
    ("vram_pct", "vram_utilization", "VRAM utilization (pmon sm)", "%", "#059669"),
    ("memory_gib", "memory", "Memory", "GiB", "#d97706"),
)
WIDTH = 1000
HEIGHT = 340
LEFT, RIGHT, TOP, BOTTOM = 85, 25, 85, 55


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _escape(value):
    return html.escape(str(value), quote=True)


def _fixed_sig(value, digits):
    """Format with ``digits`` significant figures, but never in exponent form."""
    text = format(value, "." + str(digits) + "g")
    marker = text.find("e")
    if marker >= 0:
        exponent = int(text[marker + 1:])
        decimals = max(0, digits - 1 - exponent)
        text = format(value, "." + str(decimals) + "f")
    return "0" if text.startswith("-") and float(text) == 0 else text


def _y_tick_labels(values, unit):
    """Label y ticks with 2 significant figures, adding digits only to stay distinct."""
    for digits in (2, 3, 4, 5):
        labels = [_fixed_sig(value, digits) for value in values]
        if len(set(labels)) == len(labels):
            break
    suffix = "%" if unit == "%" else ""
    return [label + suffix for label in labels]


def _fmt_minutes(seconds, span_seconds=None):
    """Format an elapsed time (seconds) as a plain decimal minute tick label.

    The first sample rarely lands on exactly 0 s, so the leftmost tick can be a
    value like 1.6e-4 s. Anything that small relative to the axis span is
    labelled ``0``; every other value stays in plain decimals so the axis never
    falls back to scientific notation.
    """
    minutes = seconds / 60.0
    if span_seconds is not None and abs(seconds) < abs(span_seconds) / 2000.0:
        return "0"
    magnitude = abs(minutes)
    if magnitude == 0:
        return "0"
    if magnitude >= 100:
        return format(minutes, ".0f")
    if magnitude >= 1:
        return format(minutes, ".1f").rstrip("0").rstrip(".")
    decimals = min(9, 2 - int(math.floor(math.log10(magnitude))))
    return format(minutes, "." + str(decimals) + "f").rstrip("0").rstrip(".")


def _reduce(points, xmin, xmax, pixels):
    """Reduce (elapsed, value, segment, row_index) without crossing gaps.

    Selection is global per pixel, rather than per segment, bounding even a
    pathological sequence of alternating valid/missing samples. Segment IDs
    survive selection so removed gaps can never create a connecting line.
    """
    buckets = {}
    span = xmax - xmin
    for point in points:
        pixel = min(pixels - 1, max(0, int((point[0] - xmin) / span * pixels))) if span else 0
        bucket = buckets.get(pixel)
        if bucket is None:
            buckets[pixel] = [point, point, point, point]
        else:
            bucket[3] = point
            if point[1] < bucket[1][1]:
                bucket[1] = point
            if point[1] > bucket[2][1]:
                bucket[2] = point
    selected = {point[3]: point for bucket in buckets.values() for point in bucket}
    return sorted(selected.values(), key=lambda p: (p[0], p[3]))


def _panel(rows, metric, containers, yoffset=0):
    column, _, label, unit, color = metric
    plot_width = WIDTH - LEFT - RIGHT
    plot_height = HEIGHT - TOP - BOTTOM
    elapsed = [row["elapsed_s"] for row in rows if row["elapsed_s"] is not None]
    xmin, xmax = (min(elapsed), max(elapsed)) if elapsed else (0.0, 1.0)
    values = [row[column] for row in rows if row[column] is not None]
    ymin = min(0.0, min(values)) if values else 0.0
    ymax = max(0.0, max(values)) if values else 1.0
    if ymin == ymax:
        ymax = ymin + 1.0
    # Normalize before subtracting: finite inputs may still have an overflowing range.
    yscale = max(abs(ymin), abs(ymax), 1.0)
    low, high = ymin / yscale, ymax / yscale
    xscale = max(abs(xmin), abs(xmax), 1.0)
    xlow, xhigh = xmin / xscale, xmax / xscale

    def xy(x, y):
        px = LEFT + ((x / xscale - xlow) / (xhigh - xlow) if xhigh != xlow else .5) * plot_width
        py = TOP + (high - y / yscale) / (high - low) * plot_height
        return f"{px:.2f},{py:.2f}"

    # One colour per container when several are monitored; a single container
    # keeps the metric colour so existing charts look unchanged.
    multi = len(containers) > 1
    palette = ("#2563eb", "#d97706", "#059669", "#7c3aed", "#dc2626", "#0891b2", "#65a30d")
    parts = [f'<g transform="translate(0,{yoffset})">',
             f'<rect width="{WIDTH}" height="{HEIGHT}" fill="white"/>',
             f'<text x="{LEFT}" y="25" font-size="19" font-weight="bold">{_escape(label)} ({_escape(unit)})</text>']
    if multi:
        legend_x = float(LEFT)
        for index_c, container_c in enumerate(containers):
            chip = palette[index_c % len(palette)]
            parts.append(f'<rect x="{legend_x:.2f}" y="37" width="11" height="11" rx="2" fill="{chip}"/>')
            legend_x += 15
            parts.append(f'<text x="{legend_x:.2f}" y="47" font-size="11">{_escape(container_c)}</text>')
            legend_x += 6.3 * len(container_c) + 18
    else:
        parts.append(f'<text x="{LEFT}" y="45" font-size="11">Containers: '
                     f'{_escape(", ".join(containers) or "(none)")}</text>')
    if values:
        peak = max((row for row in rows if row[column] is not None), key=lambda row: row[column])
        # Peak caption keeps 4 significant figures; integer-sized values stay exact,
        # while the axis ticks above are rounded to 2 significant figures.
        peak_text = (f'Peak: {_fixed_sig(peak[column], 4)} {unit} at {peak["timestamp"] or "unknown time"}'
                     f' | container: {peak["container"]}')
    else:
        peak_text = "No valid samples (No valid data)"
    parts.append(f'<text x="{LEFT}" y="65" font-size="12">{_escape(peak_text)}</text>')
    parts.append(f'<text x="{LEFT}" y="80" font-size="11">Valid samples: {len(values)} | Missing samples: {len(rows) - len(values)}</text>')
    y_values = [((low * (1 - i / 4) + high * (i / 4))) * yscale for i in range(5)]
    y_labels = _y_tick_labels(y_values, unit)
    for i, tick_label in enumerate(y_labels):
        fraction = i / 4
        y = TOP + plot_height * (1 - fraction)
        parts.extend((f'<path d="M{LEFT},{y:.2f} H{WIDTH - RIGHT}" stroke="#e2e8f0"/>',
                      f'<text x="{LEFT - 9}" y="{y + 4:.2f}" text-anchor="end" font-size="11">{_escape(tick_label)}</text>'))
        x = LEFT + plot_width * fraction
        tick = (xlow * (1 - fraction) + xhigh * fraction) * xscale
        tick_text = _fmt_minutes(tick, abs(xmax - xmin))
        parts.append(f'<text x="{x:.2f}" y="{TOP + plot_height + 20}" text-anchor="middle" font-size="11">{_escape(tick_text)}</text>')
    parts.extend((f'<path d="M{LEFT},{TOP} V{TOP + plot_height} H{WIDTH - RIGHT}" fill="none" stroke="#64748b"/>',
                  f'<text x="{LEFT + plot_width / 2}" y="{HEIGHT - 9}" text-anchor="middle" font-size="12">Elapsed time (min)</text>'))
    for index_c, container in enumerate(containers):
        line_color = color if not multi else palette[index_c % len(palette)]
        points = []
        segment = 0
        for index, row in enumerate(rows):
            if row["container"] != container:
                continue
            if row[column] is None or row["elapsed_s"] is None:
                segment += 1
                continue
            points.append((row["elapsed_s"], row[column], segment, index))
        # Normalize x for reduction as well, avoiding overflow for extreme finite inputs.
        normalized = [(p[0] / xscale, *p[1:]) for p in points]
        reduced = _reduce(normalized, xlow, xhigh, plot_width)
        groups = []
        for point in reduced:
            if not groups or groups[-1][-1][2] != point[2]:
                groups.append([])
            groups[-1].append(point)
        parts.append(f'<g aria-label="{_escape(container)}" stroke="{line_color}" fill="none">')
        for group in groups:
            coords = [xy(p[0] * xscale, p[1]) for p in group]
            if len(coords) == 1:
                x, y = coords[0].split(",")
                parts.append(f'<circle cx="{x}" cy="{y}" r="2" fill="{line_color}"/>')
            else:
                parts.append(f'<polyline points="{" ".join(coords)}" stroke-width="1.5"/>')
        parts.append('</g>')
    if values and peak["elapsed_s"] is not None:
        x, y = xy(peak["elapsed_s"], peak[column]).split(",")
        # Keep the label inside the plot even when the peak is its last sample.
        label_x = float(x) - 9 if float(x) > WIDTH - RIGHT - 65 else float(x) + 9
        anchor = "end" if float(x) > WIDTH - RIGHT - 65 else "start"
        label_y = float(y) + 18 if float(y) < TOP + plot_height - 25 else float(y) - 12
        tooltip = (f'Peak {label}: {peak[column]} {unit} | timestamp: {peak["timestamp"] or "unknown time"}'
                   f' | elapsed: {_fmt_minutes(peak["elapsed_s"])} min ({peak["elapsed_s"]} s)'
                   f' | container: {peak["container"]}')
        parts.extend((f'<g class="peak-marker" aria-label="Peak {_escape(label)}">',
                      f'<title>{_escape(tooltip)}</title>',
                      f'<path d="M{x},{y} V{TOP + plot_height}" stroke="#dc2626" stroke-dasharray="4 4"/>',
                      f'<circle class="peak-point" cx="{x}" cy="{y}" r="4" fill="#dc2626" stroke="white" stroke-width="1"/>',
                      f'<text x="{label_x:.2f}" y="{label_y:.2f}" text-anchor="{anchor}" font-size="12" font-weight="bold" style="fill:#dc2626">Peak</text>',
                      '</g>'))
    parts.append('</g>')
    return "\n".join(parts)


def _svg(panels, height):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
            f'viewBox="0 0 {WIDTH} {height}" role="img">\n'
            '<style>text { font-family: sans-serif; fill: #0f172a; }</style>\n'
            + "\n".join(panels) + '\n</svg>\n')


def _library_candidates(name):
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


def _png_backend():
    """Bind installed librsvg/Cairo; no pip package or external command needed."""
    import ctypes as c
    if hasattr(_png_backend, 'cached'):
        return _png_backend.cached
    libraries = {}
    for name in ('rsvg-2', 'cairo', 'gobject-2.0', 'glib-2.0'):
        libraries[name] = _load_library(name)
    rsvg, cairo = libraries['rsvg-2'], libraries['cairo']
    signatures = [
        (rsvg.rsvg_handle_new_from_data, c.c_void_p, [c.c_void_p, c.c_size_t, c.POINTER(c.c_void_p)]),
        (rsvg.rsvg_handle_render_cairo, c.c_int, [c.c_void_p, c.c_void_p]),
        (libraries['gobject-2.0'].g_object_unref, None, [c.c_void_p]),
        (libraries['glib-2.0'].g_error_free, None, [c.c_void_p]),
        (cairo.cairo_image_surface_create, c.c_void_p, [c.c_int, c.c_int, c.c_int]),
        (cairo.cairo_surface_status, c.c_int, [c.c_void_p]),
        (cairo.cairo_surface_destroy, None, [c.c_void_p]),
        (cairo.cairo_create, c.c_void_p, [c.c_void_p]),
        (cairo.cairo_status, c.c_int, [c.c_void_p]),
        (cairo.cairo_destroy, None, [c.c_void_p]),
        (cairo.cairo_scale, None, [c.c_void_p, c.c_double, c.c_double]),
        (cairo.cairo_surface_write_to_png, c.c_int, [c.c_void_p, c.c_char_p]),
    ]
    for function, result, arguments in signatures:
        function.restype, function.argtypes = result, arguments
    _png_backend.cached = (c, libraries)
    return _png_backend.cached


def _write_png(svg, path, height=HEIGHT, scale=2):
    """Rasterize our generated SVG to an actual high-resolution PNG image."""
    import os
    c, libraries = _png_backend()
    rsvg, cairo = libraries['rsvg-2'], libraries['cairo']
    data = svg.encode('utf-8')
    buffer = c.create_string_buffer(data)
    error = c.c_void_p()
    handle = rsvg.rsvg_handle_new_from_data(buffer, len(data), c.byref(error))
    if error:
        libraries['glib-2.0'].g_error_free(error)
    if not handle:
        raise RuntimeError('librsvg could not parse generated chart')
    surface = context = None
    try:
        surface = cairo.cairo_image_surface_create(0, WIDTH * scale, height * scale)
        if not surface or cairo.cairo_surface_status(surface):
            raise RuntimeError('Cairo could not allocate PNG surface')
        context = cairo.cairo_create(surface)
        if not context or cairo.cairo_status(context):
            raise RuntimeError('Cairo could not create PNG drawing context')
        cairo.cairo_scale(context, scale, scale)
        if not rsvg.rsvg_handle_render_cairo(handle, context) or cairo.cairo_status(context):
            raise RuntimeError('librsvg failed to draw PNG chart')
        status = cairo.cairo_surface_write_to_png(surface, os.fsencode(path))
        if status:
            raise RuntimeError('PNG write failed (Cairo status {})'.format(status))
    finally:
        if context:
            cairo.cairo_destroy(context)
        if surface:
            cairo.cairo_surface_destroy(surface)
        libraries['gobject-2.0'].g_object_unref(handle)


def load_rows(csv_path):
    """Read the sampling CSV into sorted rows plus the ordered container list."""
    rows = []
    with Path(csv_path).open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            row = {key: _number(raw.get(key)) for key in ("elapsed_s", *(m[0] for m in METRICS))}
            row.update(timestamp=raw.get("timestamp") or "", container=raw.get("container") or "(unknown)")
            rows.append(row)
    rows.sort(key=lambda row: (row["elapsed_s"] is None, row["elapsed_s"] or 0.0))
    return rows, sorted({row["container"] for row in rows})


def chart_svgs(rows, containers):
    """Return [(stem, svg, pixel_height)] for the four metrics and the overview.

    Only the PNG rasterizer consumes these; no SVG file is written to disk.
    """
    charts = []
    panels = []
    for metric in METRICS:
        panel = _panel(rows, metric, containers)
        charts.append((metric[1], _svg([panel], HEIGHT), HEIGHT))
        panels.append(_panel(rows, metric, containers, len(panels) * HEIGHT))
    charts.append(("overview", _svg(panels, HEIGHT * len(METRICS)), HEIGHT * len(METRICS)))
    return charts


def render(csv_path, output_dir):
    """Write four PNG charts, overview.png and summary.json; return the summary.

    ``summary['metrics']`` is keyed by original CSV metric column. A metric
    with no finite measurements has max/peak_timestamp/peak_elapsed_s = None.
    Ties select the first peak in elapsed-time order (CSV order breaks ties).
    Status/error do not invalidate otherwise usable partial measurements.
    """
    rows, containers = load_rows(csv_path)
    summary = {"sample_count": len(rows), "containers": containers, "metrics": {}}
    for column, _, label, unit, _ in METRICS:
        valid = [row for row in rows if row[column] is not None]
        peak = max(valid, key=lambda row: row[column]) if valid else None
        summary["metrics"][column] = {
            "label": label, "unit": unit, "valid_samples": len(valid),
            "missing_samples": len(rows) - len(valid),
            "max": peak[column] if peak else None,
            "peak_timestamp": peak["timestamp"] if peak else None,
            "peak_elapsed_s": peak["elapsed_s"] if peak else None,
            "peak_container": peak["container"] if peak else None,
        }
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    for stem, svg, height in chart_svgs(rows, containers):
        _write_png(svg, output / f"{stem}.png", height=height)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", help="Sampling CSV")
    parser.add_argument("--output-dir", required=True, help="Directory for PNG and JSON reports")
    args = parser.parse_args(argv)
    render(args.csv, args.output_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

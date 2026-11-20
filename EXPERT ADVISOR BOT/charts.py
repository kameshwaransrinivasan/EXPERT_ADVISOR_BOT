# =========================================
# charts.py — Dependency-free chart rendering
# Inline SVG/HTML only — no matplotlib, so no
# extra install needed and renders reliably in
# HTML email clients (Gmail, Outlook web, etc).
# =========================================

import datetime
import calendar


def _scale(values, out_lo, out_hi):
    if not values:
        return []
    lo, hi = min(values), max(values)
    if hi == lo:
        mid = (out_lo + out_hi) / 2
        return [mid for _ in values]
    return [out_lo + (v - lo) / (hi - lo) * (out_hi - out_lo) for v in values]


def line_chart_svg(values, title="", width=760, height=220,
                    color="#4f8cff", fill=True, y_prefix="$"):
    """Single-series SVG line/area chart (equity curve, drawdown, etc)."""
    if not values or len(values) < 2:
        return f"<p style='color:#888;font-family:Arial'>Not enough data yet for {title or 'this chart'}.</p>"

    pad_l, pad_r, pad_t, pad_b = 55, 20, 30, 26
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    xs = [pad_l + i * (plot_w / (len(values) - 1)) for i in range(len(values))]
    ys = _scale(values, pad_t + plot_h, pad_t)  # SVG y grows downward
    points = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))

    lo, hi = min(values), max(values)
    zero_line = ""
    if lo < 0 < hi:
        zero_y = pad_t + plot_h - ((0 - lo) / (hi - lo)) * plot_h
        zero_line = (f'<line x1="{pad_l}" y1="{zero_y:.1f}" x2="{pad_l+plot_w}" '
                     f'y2="{zero_y:.1f}" stroke="#bbb" stroke-dasharray="4,3"/>')

    area = ""
    if fill:
        area_pts = f"{xs[0]:.1f},{pad_t+plot_h:.1f} {points} {xs[-1]:.1f},{pad_t+plot_h:.1f}"
        area = f'<polygon points="{area_pts}" fill="{color}" opacity="0.12"/>'

    end_color = "#1a9d4b" if values[-1] >= values[0] else "#d9432e"

    return f"""
<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}"
     xmlns="http://www.w3.org/2000/svg"
     style="background:#fff;border:1px solid #eee;border-radius:6px">
  <text x="{pad_l}" y="18" font-family="Arial" font-size="13" fill="#333" font-weight="bold">{title}</text>
  {zero_line}
  {area}
  <polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.2"/>
  <circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="3.5" fill="{end_color}"/>
  <text x="{pad_l-48}" y="{pad_t+4}" font-family="Arial" font-size="10" fill="#888">{y_prefix}{hi:,.0f}</text>
  <text x="{pad_l-48}" y="{pad_t+plot_h}" font-family="Arial" font-size="10" fill="#888">{y_prefix}{lo:,.0f}</text>
  <text x="{pad_l}" y="{height-8}" font-family="Arial" font-size="10" fill="#888">Start: {y_prefix}{values[0]:,.0f}</text>
  <text x="{width-pad_r-110}" y="{height-8}" font-family="Arial" font-size="10" fill="#888" text-anchor="end">Now: {y_prefix}{values[-1]:,.0f}</text>
</svg>
"""


def heatmap_html(cell_data: dict, title="Trade Heatmap (Hour × Day)"):
    """
    cell_data: {(weekday 0=Mon..6=Sun, hour 0-23): {"trades": n, "profit": p}}
    Renders as a 24-row x 7-col HTML table shaded by trade count —
    works in email clients that would drop SVG/JS.
    """
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    max_trades = max((v["trades"] for v in cell_data.values()), default=0) or 1

    header = "<tr><th style='padding:3px;font-size:10px;color:#888'></th>" + "".join(
        f"<th style='padding:3px;font-size:10px;color:#888'>{d}</th>" for d in days
    ) + "</tr>"

    rows = ""
    for hour in range(24):
        row_has_data = any(cell_data.get((wd, hour), {}).get("trades", 0) for wd in range(7))
        if not row_has_data:
            continue
        cells = f"<td style='padding:2px 4px;font-size:10px;color:#888;text-align:right'>{hour:02d}h</td>"
        for wd in range(7):
            c = cell_data.get((wd, hour), {"trades": 0, "profit": 0})
            n = c["trades"]
            if n:
                alpha = round(0.25 + 0.65 * (n / max_trades), 2)
                bg = f"rgba(79,140,255,{alpha})"
                label = f"{n}"
            else:
                bg, label = "#f7f7f7", ""
            profit_str = f"-${abs(c['profit']):.0f}" if c['profit'] < 0 else f"${c['profit']:.0f}"
            cells += (f"<td title='{n} trades, {profit_str}' "
                      f"style='width:26px;height:20px;background:{bg};"
                      f"border:1px solid #fff;text-align:center;font-size:9px;"
                      f"color:#333;font-family:Arial'>{label}</td>")
        rows += f"<tr>{cells}</tr>"

    if not rows:
        return "<p style='color:#888;font-family:Arial'>Not enough trades yet to build an hourly heatmap.</p>"

    return (f"<p style='font-family:Arial;font-size:13px;font-weight:bold;"
            f"margin-bottom:4px'>{title}</p>"
            f"<table style='border-collapse:collapse'>{header}{rows}</table>")


def month_calendar_html(daily_profit: dict, year: int, month: int):
    """daily_profit: {datetime.date: profit_float} -> monthly P&L calendar grid."""
    cal = calendar.Calendar(firstweekday=0)  # Monday-start weeks
    weeks = cal.monthdayscalendar(year, month)
    days_hdr = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

    header = "".join(
        f"<th style='padding:6px;font-size:11px;background:#1a1a2e;color:#fff'>{d}</th>"
        for d in days_hdr
    )
    body = ""
    for week in weeks:
        row = ""
        for day in week:
            if day == 0:
                row += "<td style='background:#fafafa;border:1px solid #eee'></td>"
                continue
            d = datetime.date(year, month, day)
            profit = daily_profit.get(d)
            if profit is None:
                bg, txt = "#ffffff", ""
            elif profit > 0:
                bg, txt = "#d9f2df", f"+${profit:,.0f}"
            elif profit < 0:
                bg, txt = "#fbdede", f"-${abs(profit):,.0f}"
            else:
                bg, txt = "#f0f0f0", "$0"
            row += (f"<td style='padding:6px;border:1px solid #eee;background:{bg};"
                    f"font-size:10px;vertical-align:top;font-family:Arial'>"
                    f"<b>{day}</b><br>{txt}</td>")
        body += f"<tr>{row}</tr>"

    return f"<table style='border-collapse:collapse;width:100%'><tr>{header}</tr>{body}</table>"

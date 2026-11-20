# =========================================
# email_report.py — Automated Report Engine
# EA BOT v2 | SRD Sections 7 & 8
#
# Schedule:
#   Daily   Mon-Sat @ 00:00 UTC  -> covers the calendar day that just ended
#   Weekly  Every Sat @ 00:00 UTC -> covers Mon 00:00 -> now (the finished trading week)
#   Monthly Day 1    @ 00:00 UTC -> covers the full PREVIOUS calendar month
#
# Content: full performance dashboard (overall / risk / trading stats /
# session / symbol / pattern / strategy / errors / AI suggestions /
# equity+drawdown charts), plus a monthly P&L calendar on the monthly report.
# =========================================

import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone

import MetaTrader5 as mt5

from ai_learning import analyze_sl_hits
from risk        import dd_protection
import analytics as A
import charts    as C

# ---- CONFIG — fill these in ----
SENDER_EMAIL   = os.getenv("BOT_EMAIL",    "elitebotreports@gmail.com")
APP_PASSWORD   = os.getenv("BOT_PASS",     "sbgefukuubewscqo")
RECEIVER_EMAIL = os.getenv("BOT_RECV",     "kameshwaranfx@gmail.com")

_PLACEHOLDERS = {"your_email@gmail.com", "your_app_password"}

def _configured() -> bool:
    return (SENDER_EMAIL not in _PLACEHOLDERS
            and APP_PASSWORD not in _PLACEHOLDERS
            and RECEIVER_EMAIL not in _PLACEHOLDERS)


if not _configured():
    print("⚠️⚠️⚠️ EMAIL NOT CONFIGURED — set BOT_EMAIL, BOT_PASS "
          "(Gmail App Password, not your normal password), and BOT_RECV "
          "env vars. All email reports are DISABLED until this is set "
          "(won't spam this warning per-report). ⚠️⚠️⚠️")


# =========================================
# SMALL HELPERS
# =========================================
def _account_status() -> str:
    a = mt5.account_info()
    if a is None:
        return "MT5 not connected"
    floating = round(a.equity - a.balance, 2)
    floating_str = f"-${abs(floating)}" if floating < 0 else f"${floating}"
    return (f"Balance: ${round(a.balance, 2)} | "
            f"Equity: ${round(a.equity, 2)} | "
            f"Floating: {floating_str}")


def _html_table(rows: list, headers: list) -> str:
    th = "".join(f"<th style='padding:6px'>{h}</th>" for h in headers)
    trs = ""
    for row in rows:
        tds = "".join(f"<td style='padding:5px 8px'>{v}</td>" for v in row)
        trs += f"<tr>{tds}</tr>"
    return (
        "<table border='1' cellpadding='4' cellspacing='0' "
        "style='border-collapse:collapse;font-family:Arial,monospace;font-size:13px'>"
        f"<tr style='background:#1a1a2e;color:#fff'>{th}</tr>"
        f"{trs}</table>"
    )


def _stat_grid(pairs: list) -> str:
    """pairs: list of (label, value) -> small stat-box grid."""
    cells = ""
    for label, value in pairs:
        cells += (
            "<div style='display:inline-block;width:30%;min-width:150px;"
            "margin:4px 1.5%;padding:10px;background:#f7f8fa;"
            "border-radius:6px;box-sizing:border-box;vertical-align:top'>"
            f"<div style='font-size:11px;color:#888'>{label}</div>"
            f"<div style='font-size:16px;font-weight:bold;color:#1a1a2e'>{value}</div>"
            "</div>"
        )
    return f"<div>{cells}</div>"


def _fmt_money(v) -> str:
    return f"-${abs(v):,.2f}" if v < 0 else f"${v:,.2f}"


def _fmt_opt(v, suffix="") -> str:
    return "N/A" if v is None else f"{v:,.2f}{suffix}"


# =========================================
# SECTION RENDERERS
# =========================================
def _overall_html(o: dict) -> str:
    pf = _fmt_opt(o["profit_factor"])
    return f"""
    <h2>📊 Overall Performance</h2>
    {_stat_grid([
        ("Total Trades", o["total_trades"]),
        ("Winning Trades", o["wins"]),
        ("Losing Trades", o["losses"]),
        ("Win Rate", f"{o['win_rate']}%"),
        ("Total Profit", _fmt_money(o["total_profit"])),
        ("Total Loss", _fmt_money(o["total_loss"])),
        ("Net Profit", _fmt_money(o["net_profit"])),
        ("Profit Factor", pf),
    ])}
    """


def _risk_html(r: dict, trailing: dict) -> str:
    return f"""
    <h2>💰 Risk Analysis</h2>
    {_stat_grid([
        ("Risk / Trade (avg loss, $)", _fmt_money(r["risk_per_trade_usd"])
            if r["risk_per_trade_usd"] is not None else "N/A"),
        ("Risk / Trade (% balance)", _fmt_opt(r["risk_per_trade_pct"], "%")),
        ("Average RR", _fmt_opt(r["avg_rr"])),
        ("Max Drawdown", _fmt_opt(r["max_drawdown_pct"], "%")),
        ("Largest Win", _fmt_money(r["largest_win"])),
        ("Largest Loss", _fmt_money(r["largest_loss"])),
        ("Today's Loss", _fmt_money(trailing["daily"])),
        ("This Week's Loss", _fmt_money(trailing["weekly"])),
        ("This Month's Loss", _fmt_money(trailing["monthly"])),
    ])}
    <p style='font-size:11px;color:#999'>Risk/trade is estimated from realized
    losses (avg losing trade) since your fixed-SL, no-partial-exit design makes
    that a reliable proxy. Avg RR only counts trades opened after ticket-based
    tracking was added — older trades show as N/A, not zero.</p>
    """


def _stats_html(s: dict) -> str:
    dur = f"{s['avg_duration_min']:.0f} min" if s["avg_duration_min"] is not None else "N/A"
    hrs = f"{s['total_trading_hours']:.1f} hrs" if s["total_trading_hours"] is not None else "N/A"
    return f"""
    <h2>📈 Trading Statistics</h2>
    {_stat_grid([
        ("Average Win", _fmt_money(s["avg_win"])),
        ("Average Loss", _fmt_money(s["avg_loss"])),
        ("Consecutive Wins (max)", s["max_consec_wins"]),
        ("Consecutive Losses (max)", s["max_consec_losses"]),
        ("Avg Trade Duration", dur),
        ("Total Trading Hours", hrs),
    ])}
    """


def _ranked_table_html(rows: list, name_col: str) -> str:
    if not rows:
        return "<p style='color:#888'>No data for this period.</p>"
    table_rows = [[r["name"], r["trades"], f"{r['win_rate']}%", _fmt_money(r["profit"])]
                  for r in rows]
    return _html_table(table_rows, [name_col, "Trades", "Win %", "Profit"])


def _errors_html(e: dict) -> str:
    if e["total"] == 0:
        return "<h2>⚠️ Error Analysis</h2><p>No execution errors this period ✅</p>"
    rows = [[cat, n] for cat, n in e["by_category"]]
    samples_html = ""
    for cat, examples in e.get("samples", {}).items():
        if examples:
            ex_list = "".join(f"<li style='font-size:11px;color:#666'>{x}</li>" for x in examples)
            samples_html += f"<p style='font-size:12px;margin:6px 0 2px'><b>{cat}</b> examples:</p><ul style='margin-top:0'>{ex_list}</ul>"
    return f"""
    <h2>⚠️ Error Analysis</h2>
    <p>Total logged issues: <b>{e['total']}</b></p>
    {_html_table(rows, ["Category", "Count"])}
    {samples_html}
    """


def _suggestions_html(suggestions: list) -> str:
    items = "".join(f"<li>{s}</li>" for s in suggestions) if suggestions else "<li>No notable patterns this period</li>"
    return f"""
    <h2>🤖 AI Suggestions</h2>
    <blockquote style='background:#f5f5dc;padding:10px;border-left:4px solid orange'>
    <em>⚠️ Advisory only — no automatic changes are applied.</em>
    </blockquote>
    <ul>{items}</ul>
    """


def _mistake_html(df, title="🔍 Mistake Analyzer") -> str:
    sl_hits = analyze_sl_hits(df) if not df.empty else []
    rows = [[h["pair"], h["mode"], h["pattern"], h["reason"], _fmt_money(h["profit"])]
            for h in sl_hits[:15]]
    table = _html_table(rows, ["Pair", "Mode", "Pattern", "SL Hit Reason", "P&L"]) \
        if rows else "<p>No SL hits this period ✅</p>"
    return f"<h2>{title}</h2>{table}"


def _charts_html(dash: dict, include_calendar=False, year=None, month=None) -> str:
    equity_svg   = C.line_chart_svg(dash["equity_curve"], title="Balance / Equity Curve", color="#4f8cff")
    drawdown_svg = C.line_chart_svg(dash["drawdown_curve"], title="Drawdown %", color="#d9432e", y_prefix="")
    heatmap      = C.heatmap_html(dash["heatmap"])
    cal_html = ""
    if include_calendar and year and month:
        cal_html = f"<h2>📅 Monthly Profit Calendar</h2>{C.month_calendar_html(dash['daily_profit'], year, month)}"
    return f"""
    <h2>📉 Equity & Drawdown</h2>
    {equity_svg}
    <br>{drawdown_svg}
    <h2>⏱️ Trade Timing</h2>
    {heatmap}
    {cal_html}
    """


def _dashboard_body(dash: dict, df, include_calendar=False, year=None, month=None) -> str:
    return f"""
    {_overall_html(dash["overall"])}
    {_risk_html(dash["risk"], dash["trailing_loss"])}
    {_stats_html(dash["stats"])}
    <h2>⏰ Session Analysis</h2>
    {_ranked_table_html(dash["sessions"], "Session")}
    <h2>💱 Symbol Analysis</h2>
    {_ranked_table_html(dash["symbols"], "Symbol")}
    <h2>📅 Strategy Analysis</h2>
    {_ranked_table_html(dash["strategies"], "Strategy")}
    <h2>🧠 Pattern Analysis</h2>
    <p style='font-size:11px;color:#999'>Grouped by tracked candle/setup pattern
    tag — this codebase doesn't yet tag trades by zone-type (Order Block / FVG /
    Liquidity Sweep) separately from candle pattern.</p>
    {_ranked_table_html(dash["patterns"], "Pattern")}
    {_mistake_html(df)}
    {_errors_html(dash["errors"])}
    {_charts_html(dash, include_calendar, year, month)}
    {_suggestions_html(dash["suggestions"])}
    """


# =========================================
# DAILY REPORT — the calendar day that just ended
# =========================================
def build_daily_report() -> str:
    now = datetime.now()  # naive local time — matches trade_memory.csv timestamps
    full_df = A.load_dashboard_df()
    start, end = A.get_previous_day_range(now)
    df = A.filter_range(full_df, start, end)
    errs_full = A.load_errors_df()
    errs = A.filter_range(errs_full, start, end) if not errs_full.empty else errs_full

    dd = dd_protection()

    if df.empty:
        body = (f"<p>No closed trades on {start.strftime('%Y-%m-%d')}.</p>"
                f"<p>DD Status: <b>{dd['status']}</b> ({dd['dd']}%)</p>"
                f"<p>Account: {_account_status()}</p>")
        return _wrap_html("🤖 EA BOT — Daily Report", body)

    dash = A.compute_dashboard(df, errs, full_df=full_df, now=now)

    body = f"""
    <p>Report for: <b>{start.strftime('%A, %B %d, %Y')}</b></p>
    <p>DD Status: <b>{dd['status']}</b> ({dd['dd']}%) | Account: {_account_status()}</p>
    {_dashboard_body(dash, df)}
    """
    return _wrap_html("🤖 EA BOT — Daily Report", body)


# =========================================
# WEEKLY REPORT — Monday 00:00 -> now (the finished trading week)
# =========================================
def build_weekly_report() -> str:
    now = datetime.now()  # naive local time — matches trade_memory.csv timestamps
    full_df = A.load_dashboard_df()
    start, end = A.get_current_week_range(now)
    df = A.filter_range(full_df, start, end)
    errs_full = A.load_errors_df()
    errs = A.filter_range(errs_full, start, end) if not errs_full.empty else errs_full

    dd = dd_protection()
    week_label = f"{start.strftime('%b %d')} – {end.strftime('%b %d, %Y')}"

    if df.empty:
        body = (f"<p>No closed trades for week of {week_label}.</p>"
                f"<p>DD Status: <b>{dd['status']}</b> ({dd['dd']}%)</p>")
        return _wrap_html("🤖 EA BOT — Weekly Report", body)

    dash = A.compute_dashboard(df, errs, full_df=full_df, now=now)

    body = f"""
    <p>Week of: <b>{week_label}</b></p>
    <p>DD Status: <b>{dd['status']}</b> ({dd['dd']}%) | Account: {_account_status()}</p>
    {_dashboard_body(dash, df)}
    """
    return _wrap_html("🤖 EA BOT — Weekly Report", body)


# =========================================
# MONTHLY REPORT — the full PREVIOUS calendar month
# =========================================
def build_monthly_report() -> str:
    now = datetime.now()  # naive local time — matches trade_memory.csv timestamps
    full_df = A.load_dashboard_df()
    start, end = A.get_previous_month_range(now)
    df = A.filter_range(full_df, start, end)
    errs_full = A.load_errors_df()
    errs = A.filter_range(errs_full, start, end) if not errs_full.empty else errs_full

    dd = dd_protection()
    month_label = start.strftime("%B %Y")

    if df.empty:
        body = (f"<p>No closed trades for {month_label}.</p>"
                f"<p>DD Status: <b>{dd['status']}</b> ({dd['dd']}%)</p>")
        return _wrap_html("🤖 EA BOT — Monthly Report", body)

    dash = A.compute_dashboard(df, errs, full_df=full_df, now=now)

    body = f"""
    <p>Month: <b>{month_label}</b> (total for the completed month)</p>
    <p>DD Status: <b>{dd['status']}</b> ({dd['dd']}%) | Account: {_account_status()}</p>
    {_dashboard_body(dash, df, include_calendar=True, year=start.year, month=start.month)}
    """
    return _wrap_html("🤖 EA BOT — Monthly Report", body)


# =========================================
# HTML WRAPPER
# =========================================
def _wrap_html(title: str, body: str) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""
    <html><body style='font-family:Arial,sans-serif;max-width:900px;margin:auto'>
    <div style='background:#1a1a2e;color:#fff;padding:20px;border-radius:8px'>
      <h1>{title}</h1>
      <p style='color:#aaa'>Generated: {now}</p>
    </div>
    <div style='padding:20px'>{body}</div>
    <hr><p style='color:#999;font-size:12px'>
    🤖 EA BOT | All AI suggestions are advisory only — no automatic changes applied.
    </p></body></html>
    """


# =========================================
# SEND ENGINE
# =========================================
def _send(subject: str, html: str):
    if not _configured():
        print(f"📧 Email skipped (not configured): {subject}")
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["From"]    = SENDER_EMAIL
        msg["To"]      = RECEIVER_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(html, "html"))

        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(SENDER_EMAIL, APP_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())
        server.quit()
        print(f"📧 Email sent: {subject}")
    except Exception as e:
        print(f"❌ Email failed: {e}")


def send_daily_report():
    _send("🤖 EA BOT — Daily Report", build_daily_report())


def send_weekly_report():
    _send("🤖 EA BOT — Weekly Report", build_weekly_report())


def send_monthly_report():
    _send("🤖 EA BOT — Monthly Report", build_monthly_report())


# Legacy alias
def send_email_report():
    send_daily_report()

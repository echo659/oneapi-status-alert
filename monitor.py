"""Monitor the overall health AND capacity of selected status.oneapi.im channels.

Uses the visible channel summary cards, not the lower worker/model statuses.
The browser extraction is deliberately conservative: unexpected markup => no alert.
"""

from __future__ import annotations

import argparse
import json
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo


URL = "https://status.oneapi.im/"
CHANNELS = ("agentspace", "bard")
STATE_FILE = Path(__file__).parent / "state.json"
TIMEZONE = ZoneInfo("Asia/Shanghai")
FAILURE_LIMIT = 3


def log(message: str) -> None:
    print(f"[{datetime.now(TIMEZONE):%Y-%m-%d %H:%M:%S} 北京时间] {message}", flush=True)

# Read exact on-screen status and numeric values from each small channel card.
# Each text token has a real browser position; the capacity values must sit
# *below the 容量 heading and in its column*, not the adjacent 队列 column.
EXTRACT_JS = r"""
(names) => {
  function items(root) {
    const out = [];
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let n;
    while ((n = walker.nextNode())) {
      const value = (n.textContent || '').trim();
      if (!value) continue;
      const el = n.parentElement;
      if (!el || !el.getClientRects().length || getComputedStyle(el).visibility !== 'visible') continue;
      const range = document.createRange();
      range.selectNodeContents(n);
      const r = range.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      out.push({ text: value, cx: (r.left + r.right) / 2,
        top: r.top, bottom: r.bottom, left: r.left, right: r.right, el });
    }
    return out;
  }
  const all = items(document.body);
  const isTitle = (x, name) => x.text.toLowerCase() === name ||
    (name === 'agentspace' && /^agentsp(?:ace)?(?:…|\.{3})$/i.test(x.text));
  const statusLabels = ['健康', '休整中', '故障'];
  const byRow = (a,b) => Math.abs((a.top+a.bottom-b.top-b.bottom)/2) <= 10;
  const answer = {};
  for (const name of names) {
    const titleTokens = all.filter(x => isTitle(x, name));
    const matches = new Map();
    for (const token of titleTokens) {
      for (let node = token.el, depth = 0; node && depth < 12; node = node.parentElement, depth++) {
        const rect = node.getBoundingClientRect();
        if (rect.width < 130 || rect.width > 700 || rect.height < 65 || rect.height > 450) continue;
        const xs = items(node);
        const titles = xs.filter(x => isTitle(x, name));
        const capHeaders = xs.filter(x => x.text === '容量');
        const queueHeaders = xs.filter(x => x.text === '队列');
        if (titles.length !== 1 || capHeaders.length !== 1 || queueHeaders.length !== 1) continue;
        const title = titles[0], capHeader = capHeaders[0], queueHeader = queueHeaders[0];
        if (!byRow(capHeader, queueHeader) || Math.abs(capHeader.cx - queueHeader.cx) < 12) continue;
        // Only a status badge in the title row, on the right and above the table.
        const statuses = xs.filter(x => statusLabels.includes(x.text) &&
          byRow(x, title) && x.left >= title.right - 2 &&
          x.cx >= rect.left + rect.width / 2 &&
          x.bottom <= Math.min(capHeader.top, queueHeader.top) + 2 &&
          title.top <= rect.top + rect.height / 2);
        if (statuses.length !== 1) continue;
        // A parent containing two named channel cards is not a summary card.
        if (xs.some(x => names.some(other => other !== name && isTitle(x, other)))) continue;
        matches.set(node, { xs, status: statuses[0], capHeader, queueHeader, bottom: rect.bottom });
        break;
      }
    }
    if (matches.size !== 1) {
      answer[name] = { error: '顶部通道卡片或右上角状态不唯一/无法定位' };
      continue;
    }
    const card = [...matches.values()][0];
    const {capHeader, queueHeader} = card;
    const values = card.xs.filter(x =>
      /^(?:\d+(?:\.\d+)?|∞)$/.test(x.text) &&
      x.top >= capHeader.bottom - 2 &&
      x.top < card.bottom &&
      Math.abs(x.cx - capHeader.cx) < Math.abs(x.cx - queueHeader.cx) &&
      Math.abs(x.cx - capHeader.cx) < Math.abs(capHeader.cx - queueHeader.cx) / 2
    );
    if (!values.length) {
      answer[name] = { error: '无法确定容量列中的数字（为了防止误报，本次跳过）' };
      continue;
    }
    const capacities = values.map(x => x.text === '∞' ? Infinity : Number(x.text));
    const capacity = capacities.some(x => x === Infinity) ? '∞' :
      capacities.reduce((a, b) => a + b, 0);
    answer[name] = { status: card.status.text, capacity };
  }
  return answer;
}
"""


def fetch_status() -> dict:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(viewport={"width": 1600, "height": 1000}, locale="zh-CN")
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_function("document.body && document.body.innerText.includes('容量')", timeout=25000)
            result = {}
            # Retry while the dynamic dashboard finishes rendering.
            for _ in range(5):
                result = page.evaluate(EXTRACT_JS, list(CHANNELS))
                if all(not result.get(name, {}).get("error") for name in CHANNELS):
                    break
                page.wait_for_timeout(2000)
            if any(result.get(name, {}).get("error") for name in CHANNELS):
                page.screenshot(path="debug.png", full_page=True)
            return result
        except Exception:
            try:
                page.screenshot(path="debug.png", full_page=True)
            except Exception:
                pass
            raise
        finally:
            browser.close()


def smtp_port() -> int:
    raw = os.environ.get("SMTP_PORT", "").strip() or "465"
    try:
        port = int(raw)
    except ValueError:
        raise ValueError("SMTP_PORT 必须是数字") from None
    if not 1 <= port <= 65535:
        raise ValueError("SMTP_PORT 超出有效范围")
    return port


def send_email(alerts: list[tuple[str, str, str]], *, test: bool = False) -> None:
    host = os.environ.get("SMTP_HOST", "").strip()
    port = smtp_port()
    username = os.environ.get("EMAIL_USER", "").strip()
    password = os.environ.get("EMAIL_PASSWORD", "").strip()
    recipient = os.environ.get("EMAIL_TO", "").strip()
    if not all((host, username, password, recipient)):
        raise RuntimeError("未设置完整邮件参数：SMTP_HOST / EMAIL_USER / EMAIL_PASSWORD / EMAIL_TO")

    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    msg = EmailMessage()
    msg["From"] = username
    msg["To"] = recipient
    if test:
        msg["Subject"] = "【OneAPI 监控测试】邮件连接测试"
        msg.set_content(f"这是一封测试邮件，并不表示任何模型恢复。\n发送时间：{now}（北京时间）\n状态网站：{URL}")
    else:
        details = "\n".join(f"- {channel}: {status}，容量 {capacity}" for channel, status, capacity in alerts)
        msg["Subject"] = f"【模型状态提醒】{', '.join(a[0] for a in alerts)} 已健康且容量>0"
        msg.set_content(
            f"以下通道已同时满足‘整体状态=健康’且‘容量>0’：\n\n{details}\n\n"
            f"检测时间：{now}（北京时间）\n状态网站：{URL}\n\n"
            "仅在从不满足变为满足时提醒；持续符合条件不会反复发送。"
        )
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=25) as smtp:
            smtp.login(username, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=25) as smtp:
            smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(msg)


def valid_details(details: dict) -> bool:
    capacity = details.get("capacity")
    return (
        "error" not in details and details.get("status") in {"健康", "休整中", "故障"}
        and (capacity == "∞" or (type(capacity) in (int, float) and 0 <= capacity < float("inf")))
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-email", action="store_true")
    args = parser.parse_args()
    if args.test_email:
        log("开始独立测试邮件，不访问网站、不读取或修改 state.json")
        send_email([], test=True)
        log("测试邮件已交给 SMTP 服务器，请检查收件箱")
        return

    log(f"开始检查；触发方式={os.environ.get('GITHUB_EVENT_NAME', 'local')}")
    before = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    if not isinstance(before, dict):
        raise ValueError("state.json must contain a JSON object")
    failure_counts = before.get("_failure_counts", {})
    if not isinstance(failure_counts, dict):
        failure_counts = {}
    updated = dict(before)
    counts = {}
    alerts: list[tuple[str, str, str]] = []
    try:
        status = fetch_status()
    except Exception as exc:
        log(f"网页访问失败: {type(exc).__name__}: {exc}")
        status = {channel: {"error": "网页访问失败"} for channel in CHANNELS}

    for channel in CHANNELS:
        details = status.get(channel, {})
        if not valid_details(details):
            counts[channel] = max(0, int(failure_counts.get(channel, 0))) + 1
            log(f"[跳过] {channel}: {details.get('error', '数据不可靠')}；连续失败 {counts[channel]} 次，保留旧状态")
            continue
        if failure_counts.get(channel, 0):
            log(f"{channel}: 页面读取恢复，清零失败计数")
        counts[channel] = 0
        healthy = details["status"] == "健康" and (
            details["capacity"] == "∞" or details["capacity"] > 0
        )
        log(f"[检查] {channel}: {details['status']}, 容量={details['capacity']}, 符合条件={healthy}")
        if healthy and not before.get(channel, False):
            alerts.append((channel, details["status"], str(details["capacity"])))
        else:
            updated[channel] = healthy

    updated["_failure_counts"] = counts
    try:
        if alerts:
            send_email(alerts)
            for channel, _, _ in alerts:
                updated[channel] = True
            log("[邮件已发送] " + ", ".join(a[0] for a in alerts))
    finally:
        # Preserve confirmed state, including counters, even if SMTP fails.
        # Failed delivery must not mark an alert as sent.
        STATE_FILE.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    failed = [name for name in CHANNELS if counts[name] >= FAILURE_LIMIT]
    if failed:
        log("::error::连续至少 3 次无法读取：" + ", ".join(failed) + "；请查看运行日志及截图")
        raise RuntimeError("连续读取异常；已保留上次确认状态")
    if any(counts.values()):
        log("::warning::本轮存在读取异常；未误报，下一轮继续检测")
    log("检查结束")


if __name__ == "__main__":
    main()

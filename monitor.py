"""Monitor the overall health AND capacity of selected status.oneapi.im channels.

Uses the visible channel summary cards, not the lower worker/model statuses.
The browser extraction is deliberately conservative: unexpected markup => no alert.
"""

from __future__ import annotations

import json
import os
import re
import smtplib
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

URL = "https://status.oneapi.im/"
CHANNELS = ("agentspace", "bard")
STATE_FILE = Path(__file__).parent / "state.json"
TIMEZONE = ZoneInfo("Asia/Shanghai")

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
      if (!el || !el.getClientRects().length) continue;
      const range = document.createRange();
      range.selectNodeContents(n);
      const r = range.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      out.push({ text: value, cx: (r.left + r.right) / 2,
        top: r.top, bottom: r.bottom, left: r.left, right: r.right });
    }
    return out;
  }
  const all = items(document.body);
  const answer = {};
  for (const name of names) {
    const titleTokens = all.filter(x => x.text.toLowerCase() === name);
    const matches = [];
    for (const token of titleTokens) {
      // Match a DOM element that actually encloses this title text node.
      const element = document.elementFromPoint(token.cx, (token.top + token.bottom) / 2);
      for (let node = element, depth = 0; node && depth < 12; node = node.parentElement, depth++) {
        const rect = node.getBoundingClientRect();
        if (rect.width < 130 || rect.width > 700 || rect.height < 65 || rect.height > 450) continue;
        const xs = items(node);
        if (!xs.some(x => x.text === name) ||
            !xs.some(x => x.text === '容量') ||
            !xs.some(x => x.text === '队列') ||
            !xs.some(x => ['健康', '休整中', '故障'].includes(x.text))) continue;
        matches.push({ xs, area: rect.width * rect.height, bottom: rect.bottom });
        break;
      }
    }
    matches.sort((a, b) => a.area - b.area);
    if (!matches.length) {
      answer[name] = { error: '未找到包含整体状态和容量的通道卡片' };
      continue;
    }
    const card = matches[0];
    const statuses = card.xs.filter(x => ['健康', '休整中', '故障'].includes(x.text));
    const capHeader = card.xs.find(x => x.text === '容量');
    const queueHeader = card.xs.find(x => x.text === '队列');
    if (statuses.length !== 1 || !capHeader || !queueHeader) {
      answer[name] = { error: '通道状态或表头不唯一' };
      continue;
    }
    const values = card.xs.filter(x =>
      /^(?:\d+(?:\.\d+)?|∞)$/.test(x.text) &&
      x.top >= capHeader.bottom - 2 &&
      x.top < card.bottom &&
      Math.abs(x.cx - capHeader.cx) < Math.abs(x.cx - queueHeader.cx)
    );
    if (!values.length) {
      answer[name] = { error: '无法确定容量列中的数字（为了防止误报，本次跳过）' };
      continue;
    }
    const capacities = values.map(x => x.text === '∞' ? Infinity : Number(x.text));
    const capacity = capacities.some(x => x === Infinity) ? '∞' :
      capacities.reduce((a, b) => a + b, 0);
    answer[name] = { status: statuses[0].text, capacity };
  }
  return answer;
}
"""


def fetch_status() -> dict:
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


def send_email(alerts: list[tuple[str, str, str]]) -> None:
    host = os.environ.get("SMTP_HOST", "").strip()
    port = int(os.environ.get("SMTP_PORT", "465"))
    username = os.environ.get("EMAIL_USER", "").strip()
    password = os.environ.get("EMAIL_PASSWORD", "").strip()
    recipient = os.environ.get("EMAIL_TO", "").strip()
    if not all((host, username, password, recipient)):
        raise RuntimeError("未设置完整邮件参数：SMTP_HOST / EMAIL_USER / EMAIL_PASSWORD / EMAIL_TO")

    now = datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    details = "\n".join(
        f"- {channel}: {status}，容量 {capacity}" for channel, status, capacity in alerts
    )
    msg = EmailMessage()
    msg["Subject"] = f"【模型状态提醒】{', '.join(a[0] for a in alerts)} 已健康且容量>0"
    msg["From"] = username
    msg["To"] = recipient
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


def main() -> None:
    before = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    if not isinstance(before, dict):
        raise ValueError("state.json must contain a JSON object")
    status = fetch_status()
    updated = dict(before)
    alerts: list[tuple[str, str, str]] = []
    success = 0

    for channel in CHANNELS:
        details = status.get(channel, {})
        if "error" in details or "status" not in details or "capacity" not in details:
            print(f"[跳过] {channel}: {details.get('error', '数据不可用')}")
            continue  # Do not reset state or send alerts on partial read failures.
        success += 1
        healthy = details["status"] == "健康" and (
            details["capacity"] == "∞" or details["capacity"] > 0
        )
        print(f"[检查] {channel}: {details['status']}, 容量={details['capacity']}, 符合条件={healthy}")
        if healthy and not before.get(channel, False):
            alerts.append((channel, details["status"], str(details["capacity"])))
        updated[channel] = healthy

    if success == 0:
        raise RuntimeError("两个通道都无法可靠解析；没有发送邮件，也没有覆盖旧状态。见 debug.png")
    if alerts:
        send_email(alerts)  # Only persist state once an alert has been sent.
        print("[邮件已发送]", ", ".join(a[0] for a in alerts))
    STATE_FILE.write_text(json.dumps(updated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

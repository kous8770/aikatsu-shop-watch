"""アイカツ！アンコール公式サイトの取扱店舗の「最終更新」を見張り、変わったら Discord に知らせる。

環境変数
  STORES               見張る店名。1 行に 1 店（改行かカンマ区切り）。公式サイトの検索欄と同じ「おみせのなまえや住所」で探す。
  DISCORD_WEBHOOK_URL  Discord の Webhook の URL（GitHub の Secrets に入れる）
  STATE_FILE           前回の状態を保存するファイル（既定：state.json）
  DRY_RUN=1            Discord に送らず、送る内容を画面に出す（この PC で試すとき）
  NOTIFY_TEST=1        変化がなくても、今の状態を Discord に送る（手動の動作確認）

公開リポジトリのログは誰でも見られるので、DRY_RUN でないときは店名をログに出さない。
状態ファイルにも店名は書かず、店名と住所のハッシュだけを書く。
"""

import hashlib
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://dcd.aikatsu.com/encore/shoplist/list.php"
UA = "Mozilla/5.0 (compatible; aikatsu-shop-watch)"
DRY_RUN = os.environ.get("DRY_RUN") == "1"
NOTIFY_TEST = os.environ.get("NOTIFY_TEST") == "1"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
MAX_PAGES = 20


def log(msg):
    print(msg, flush=True)


def fetch(params):
    url = BASE + "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = e
            time.sleep(5 * (i + 1))
    raise RuntimeError(f"公式サイトを読めませんでした：{type(last).__name__}")


def text(s):
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_stores(page):
    """1 ページ分の店の一覧を返す。[{name, address, machines: [{no, updated, cards}]}]"""
    stores = []
    blocks = re.split(r'<div class="modal detail modalFilterCol" id="store_\d+">', page)[1:]
    for b in blocks:
        name = re.search(r'class="storeColName">(.*?)</h3>', b, re.S)
        addr = re.search(r'class="storeColAddress">(.*?)</p>', b, re.S)
        machines = []
        for item in re.split(r'<li class="runningItem">', b)[1:]:
            no = re.search(r'class="runningItemNum"><span class="num">(\d+)', item)
            upd = re.search(r'最終更新：<span title="([^"]*)"', item)
            cards = [text(c) for c in re.findall(r'<li class="runningCardItem">(.*?)</li>', item, re.S)]
            machines.append({
                "no": no.group(1) if no else str(len(machines) + 1),
                "updated": upd.group(1) if upd else "",
                "cards": [c for c in cards if c],
            })
        if name:
            stores.append({"name": text(name.group(1)), "address": text(addr.group(1)) if addr else "", "machines": machines})
    return stores


def search(query):
    found = []
    for p in range(1, MAX_PAGES + 1):
        page = fetch({"free": query, "pref": "", "city": "", "p": p})
        found += parse_stores(page)
        if f"p={p + 1}'" not in page and f"p={p + 1}\"" not in page:
            break
    # 同じ店が 2 回出ないようにする
    uniq = {}
    for s in found:
        uniq.setdefault((s["name"], s["address"]), s)
    found = list(uniq.values())
    exact = [s for s in found if s["name"] == query]
    return exact or found


def key_of(store):
    return hashlib.sha256((store["name"] + "|" + store["address"]).encode("utf-8")).hexdigest()[:16]


def short_time(t):
    m = re.match(r"\d{4}-(\d\d)-(\d\d) (\d\d:\d\d)", t or "")
    return f"{int(m.group(1))}/{int(m.group(2))} {m.group(3)}" if m else (t or "なし")


def cards_text(cards):
    return "・".join(cards) if cards else "情報なし"


def store_link(store):
    return BASE + "?" + urllib.parse.urlencode({"free": store["name"], "pref": "", "city": "", "p": 1})


def send(content):
    if DRY_RUN:
        log("---- Discord に送る内容 ----\n" + content)
        return
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        raise RuntimeError("DISCORD_WEBHOOK_URL が設定されていません")
    body = json.dumps({"content": content[:1990], "allowed_mentions": {"parse": []}}).encode("utf-8")
    for i in range(3):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3)
                continue
            raise RuntimeError(f"Discord への送信に失敗しました（{e.code}）")
    raise RuntimeError("Discord への送信に失敗しました（混雑）")


def main():
    queries = [q.strip() for q in re.split(r"[\n,，]", os.environ.get("STORES", "")) if q.strip()]
    if not queries:
        log("STORES（見張る店名）が設定されていません")
        return 1

    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    stores_state = state.setdefault("stores", {})
    missing_state = state.setdefault("missing", [])

    messages = []
    seen_keys = set()
    errors = 0
    for qi, q in enumerate(queries, 1):
        qkey = hashlib.sha256(q.encode("utf-8")).hexdigest()[:16]
        try:
            stores = search(q)
        except RuntimeError as e:
            errors += 1
            log(f"{qi} 件目の店：{e}")
            continue
        if not stores:
            if qkey not in missing_state:
                missing_state.append(qkey)
                messages.append(f"⚠️ 「{q}」が公式サイトの取扱店舗で見つかりません。店名を確かめてください。")
            log(f"{qi} 件目の店：見つからない")
            continue
        if qkey in missing_state:
            missing_state.remove(qkey)
        log(f"{qi} 件目の店：{len(stores)} 店が該当")

        for s in stores:
            k = key_of(s)
            seen_keys.add(k)
            now = {m["no"]: m["updated"] for m in s["machines"]}
            old = stores_state.get(k)
            head = f"**{s['name']}**"
            if old is None:
                lines = [f"👀 見張りを始めました：{head}（{len(s['machines'])} 台）"]
                lines += [f"　{m['no']}台目　最終更新 {short_time(m['updated'])}　排出：{cards_text(m['cards'])}" for m in s["machines"]]
                messages.append("\n".join(lines))
            else:
                lines = []
                for m in s["machines"]:
                    before = old.get(m["no"])
                    if before is None:
                        lines.append(f"　🆕 {m['no']}台目が増えました　最終更新 {short_time(m['updated'])}　排出：{cards_text(m['cards'])}")
                    elif before != m["updated"]:
                        lines.append(f"　{m['no']}台目　最終更新 {short_time(before)} → **{short_time(m['updated'])}**　排出：{cards_text(m['cards'])}")
                for no in old:
                    if no not in now:
                        lines.append(f"　➖ {no}台目が一覧から消えました")
                if lines:
                    messages.append("\n".join([f"🔔 {head} の最終更新が変わりました"] + lines + [f"<{store_link(s)}>"]))
                elif NOTIFY_TEST:
                    lines = [f"🧪 動作確認：{head}（{len(s['machines'])} 台、変化なし）"]
                    lines += [f"　{m['no']}台目　最終更新 {short_time(m['updated'])}　排出：{cards_text(m['cards'])}" for m in s["machines"]]
                    messages.append("\n".join(lines))
            stores_state[k] = now

    # 見張りをやめた店の記録は消す（公式サイトの読み込みに失敗したときは消さない）
    if not errors:
        for k in list(stores_state):
            if k not in seen_keys:
                del stores_state[k]

    for msg in messages:
        send(msg)
    log(f"通知 {len(messages)} 件")

    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_FILE)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        log(str(e))
        sys.exit(1)

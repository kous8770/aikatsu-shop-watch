"""アイカツ！アンコール公式サイトの取扱店舗の「台ごとの最終更新」を見張り、意味のある更新だけ Discord に知らせる。

知らせるもの
  - 久しぶりの更新：台の最終更新が、前回の記録から MIN_GAP_HOURS 時間以上たって新しくなった
  - 台データ復活：一覧から消えていた店（台のデータがない店は公式サイトの検索に出ない）が、新しい最終更新で戻ってきた
  - 台データ新規：見張っている地域に、見張り始めてから初めて載った店
  - 台が増えた
知らせないもの（history.csv に記録だけ）
  - 短い間隔の更新、台が消えた、店が一覧から消えた
夜（QUIET_START 時〜QUIET_END 時）は調べて記録するが、通知はためておき、朝に 1 通で送る。

環境変数
  STORES               見張る店名や地名。1 行に 1 つ。「愛知県小牧市」のように県名で始めると、その県だけで探す
  DISCORD_WEBHOOK_URL  Discord の Webhook の URL（GitHub の Secrets に入れる）
  MIN_GAP_HOURS        何時間以上ぶりの更新で知らせるか（既定：36）
  QUIET_START / QUIET_END  通知をためておく時間帯（既定：22 と 8。日本時間）
  STATE_FILE           前回の状態（既定：state.json）
  HISTORY_FILE         すべての変化の記録（既定：history.csv）
  DRY_RUN=1            Discord に送らず、送る内容を画面に出す（この PC で試すとき）
  NOTIFY_TEST=1        変化がなくても、今の状態を Discord に送る（手動の動作確認）
  FAKE_NOW             試験用。今の時刻（日本時間、"YYYY-MM-DD HH:MM"）

公開リポジトリのログは誰でも見られるので、DRY_RUN でないときは店名をログに出さない。
状態ファイルと記録にも店名は書かず、店名と住所のハッシュだけを書く。
"""

import csv
import datetime
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
HISTORY_FILE = os.environ.get("HISTORY_FILE", "history.csv")
MIN_GAP_HOURS = float(os.environ.get("MIN_GAP_HOURS") or 36)
QUIET_START = int(os.environ.get("QUIET_START") or 22)
QUIET_END = int(os.environ.get("QUIET_END") or 8)
MAX_PAGES = 20
GONE_AFTER_MISSES = 3      # 続けて 3 回（約 30 分）一覧に出なかったら「消えた」とみなす（一時的な表示の乱れで復活を誤報しないため）
FAIL_REPORT_AT = 6         # 続けて 6 回（約 1 時間）読めなかったら 1 回だけ知らせる
JST = datetime.timezone(datetime.timedelta(hours=9))
DISCLAIMER = "※更新＝筐体が通信した合図です。入荷・再開の可能性がありますが、断定はできません。"


def log(msg):
    print(msg, flush=True)


def now_jst():
    fake = os.environ.get("FAKE_NOW")
    if fake:
        return datetime.datetime.strptime(fake, "%Y-%m-%d %H:%M").replace(tzinfo=JST)
    return datetime.datetime.now(JST)


# ---------- 公式サイトを読む ----------

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


PREFS = ("北海道 青森県 岩手県 宮城県 秋田県 山形県 福島県 茨城県 栃木県 群馬県 埼玉県 千葉県 東京都 神奈川県 "
         "新潟県 富山県 石川県 福井県 山梨県 長野県 岐阜県 静岡県 愛知県 三重県 滋賀県 京都府 大阪府 兵庫県 "
         "奈良県 和歌山県 鳥取県 島根県 岡山県 広島県 山口県 徳島県 香川県 愛媛県 高知県 福岡県 佐賀県 "
         "長崎県 熊本県 大分県 宮崎県 鹿児島県 沖縄県").split()


def split_pref(query):
    """「愛知県小牧市」のように県名で始まっていれば、県の番号と残りの言葉に分ける（苫小牧市などを除くため）。"""
    for i, name in enumerate(PREFS, 1):
        if query.startswith(name):
            return f"{i:02d}", query[len(name):].strip()
    return "", query


def search(query):
    """検索結果の店を返す。ページの形が想定と違う、件数が合わないときは例外にする（誤って「消えた」と判断しないため）。"""
    pref, free = split_pref(query)
    found, total = [], None
    for p in range(1, MAX_PAGES + 1):
        page = fetch({"free": free, "pref": pref, "city": "", "p": p})
        if p == 1:
            m = re.search(r'class="resultColMsg">[^<]*<span class="data">(\d+)</span>件', page)
            if not m:
                raise RuntimeError("公式サイトのページの形がいつもと違います")
            total = int(m.group(1))
        found += parse_stores(page)
        if f"p={p + 1}'" not in page and f"p={p + 1}\"" not in page:
            break
    uniq = {}
    for s in found:
        uniq.setdefault((s["name"], s["address"]), s)
    found = list(uniq.values())
    if len(found) < total:
        raise RuntimeError(f"件数が合いません（{total} 件のはずが {len(found)} 件）")
    exact = [s for s in found if s["name"] == free]
    return exact or found


# ---------- 表示 ----------

def key_of(store):
    return hashlib.sha256((store["name"] + "|" + store["address"]).encode("utf-8")).hexdigest()[:16]


def qkey_of(query):
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]


def parse_time(t):
    try:
        return datetime.datetime.strptime(t, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def gap_hours(before, after):
    a, b = parse_time(before), parse_time(after)
    return (b - a).total_seconds() / 3600 if a and b else None


def gap_text(h):
    if h is None:
        return "間隔不明"
    return f"{h:.1f}時間ぶり" if h < 72 else f"約{h / 24:.1f}日ぶり"


def short_time(t):
    m = re.match(r"\d{4}-(\d\d)-(\d\d) (\d\d:\d\d)", t or "")
    return f"{int(m.group(1))}/{int(m.group(2))} {m.group(3)}" if m else (t or "なし")


def latest(times):
    ts = [t for t in times if parse_time(t)]
    return max(ts) if ts else ""


def describe(ev, names):
    """通知 1 件分の文。ev は店名を含まない（夜の間 state.json にためておくため）。店名はその回に読んだ一覧から引く。"""
    name = names.get(ev["k"], "（店名不明）")
    kind = ev["kind"]
    if kind == "update":
        parts = [f"{m['no']}台目：{short_time(m['before'])} → {short_time(m['after'])}、{gap_text(m['gap'])}" for m in ev["machines"]]
        return f"🟢 **{name}**（{'／'.join(parts)}）"
    if kind == "revived":
        parts = [f"{m['no']}台目：{short_time(m['after'])}" for m in ev["machines"]]
        return f"🔁 **{name}** 台データ復活（{gap_text(ev['gap'])}。前回の最終更新 {short_time(ev['before'])} → {'／'.join(parts)}）"
    if kind == "new":
        parts = [f"{m['no']}台目：{short_time(m['after'])}" for m in ev["machines"]]
        return f"🆕 **{name}** 台データ新規掲載（{len(ev['machines'])}台。{'／'.join(parts)}）"
    if kind == "machine_back":
        parts = [f"{m['no']}台目：{short_time(m['before'])} → {short_time(m['after'])}、{gap_text(m['gap'])}" for m in ev["machines"]]
        return f"🔁 **{name}** 消えていた台が戻りました（{'／'.join(parts)}）"
    if kind == "added":
        parts = [f"{m['no']}台目：{short_time(m['after'])}" for m in ev["machines"]]
        return f"➕ **{name}** 台が増えました（{'／'.join(parts)}）"
    if kind == "fail":
        return f"⚠️ 公式サイトを読めない状態が続いています（{ev['since']} から）。この間の変化は、読めるようになってから知らせます。"
    if kind == "recovered":
        return "✅ 公式サイトを読めるようになりました。"
    return ""


# ---------- 記録と送信 ----------

def write_history(rows):
    if not rows:
        return
    new = not os.path.exists(HISTORY_FILE)
    with open(HISTORY_FILE, "a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["記録した時刻", "店（ハッシュ）", "台", "前回の更新", "今回の更新", "間隔（時間）", "種類（・通知＝知らせた）"])
        w.writerows(rows)


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


def send_chunks(header, lines, footer):
    """見出し＋本文の行＋断り書きを、2000 文字以内に分けて送る。"""
    chunks, cur = [], header
    for line in lines:
        if len(cur) + 1 + len(line) > 1800:
            chunks.append(cur)
            cur = header + "（続き）"
        cur += "\n" + line
    chunks.append(cur + ("\n" + footer if footer else ""))
    for c in chunks:
        send(c)
        time.sleep(1)
    return len(chunks)


# ---------- 状態 ----------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    stores = state.get("stores", {})
    migrated = "queries" not in state
    # 古い形（店ごとに {台: 時刻} だけ）を新しい形に直す
    for k, v in list(stores.items()):
        if "m" not in v:
            stores[k] = {"m": v, "listed": True, "miss": 0, "q": ""}
    state["stores"] = stores
    state.setdefault("queries", [])
    state.setdefault("pending", [])
    state.setdefault("fail", 0)
    state.setdefault("fail_since", "")
    state.pop("missing", None)
    return state, migrated


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)
        f.write("\n")
    os.replace(tmp, STATE_FILE)


# ---------- 本体 ----------

def main():
    queries = [q.strip() for q in re.split(r"[\n,，]", os.environ.get("STORES", "")) if q.strip()]
    if not queries:
        log("STORES（見張る店名）が設定されていません")
        return 1

    state, migrated = load_state()
    st = state["stores"]
    now = now_jst()
    checked_at = now.strftime("%Y-%m-%d %H:%M")
    quiet = (now.hour >= QUIET_START or now.hour < QUIET_END) if QUIET_START != QUIET_END else False

    events, history, info_lines, test_lines = [], [], [], []
    names = {}
    errors = 0
    for qi, q in enumerate(queries, 1):
        qk = qkey_of(q)
        try:
            stores = search(q)
        except RuntimeError as e:
            errors += 1
            log(f"{qi} 件目：{e}")
            continue

        keys_now = {key_of(s) for s in stores}
        # 前の形の状態から移ったときは、もう載っている店がある地域は見張り済みとみなす
        if migrated and qk not in state["queries"] and any(k in st for k in keys_now):
            state["queries"].append(qk)
        baseline = qk not in state["queries"]
        log(f"{qi} 件目：{len(stores)} 店" + ("（見張り開始）" if baseline else ""))

        if baseline:
            if stores:
                info_lines.append(f"👀 見張りに加えました：" + "、".join(f"**{s['name']}**（{len(s['machines'])}台）" for s in stores))
            else:
                info_lines.append(f"👀 見張りに加えました：「{q}」は今、公式サイトに台のデータが載っていません。載ったら知らせます。")

        for s in stores:
            k = key_of(s)
            names[k] = s["name"]
            now_m = {m["no"]: m["updated"] for m in s["machines"]}
            old = st.get(k)
            if NOTIFY_TEST:
                test_lines.append(f"🧪 **{s['name']}**（" + "／".join(f"{m['no']}台目：{short_time(m['updated'])}" for m in s["machines"]) + "）")

            if old is None:
                if not baseline:
                    ms = [{"no": no, "after": t} for no, t in now_m.items()]
                    events.append({"kind": "new", "k": k, "machines": ms})
                    history.append([checked_at, k, "", "", latest(now_m.values()), "", "新規・通知"])
                st[k] = {"m": now_m, "listed": True, "miss": 0, "q": qk}
                continue

            if not old.get("listed", True):
                prev_all = {**old.get("gone_m", {}), **old["m"]}
                before = latest(prev_all.values())
                after = latest(now_m.values())
                newer = [{"no": no, "after": t} for no, t in now_m.items() if t and t > prev_all.get(no, "")]
                if newer:
                    gap = gap_hours(before, after)
                    events.append({"kind": "revived", "k": k, "before": before, "gap": gap, "machines": newer})
                    history.append([checked_at, k, "", before, after, "" if gap is None else f"{gap:.1f}", "復活・通知"])
                else:
                    # 最終更新が前と同じまま戻った＝表示の乱れなど。知らせない
                    history.append([checked_at, k, "", before, after, "", "再掲載（更新なし）"])
                st[k] = {"m": now_m, "listed": True, "miss": 0, "q": qk}
                continue

            ups, added, back = [], [], []
            # 店は載ったまま一部の台だけ消えることがある。消えた台の最後の時刻を覚えておき、
            # 同じ時刻で戻ったら知らせない（表示の乱れ）、新しい時刻で戻ったら「戻った」として知らせる
            gone_m = dict(old.get("gone_m", {}))
            for no, t in now_m.items():
                before = old["m"].get(no)
                if before is None:
                    last = gone_m.pop(no, None)
                    if last is None:
                        added.append({"no": no, "after": t})
                        history.append([checked_at, k, no, "", t, "", "台が増えた・通知"])
                    elif t and t > last:
                        gap = gap_hours(last, t)
                        back.append({"no": no, "before": last, "after": t, "gap": gap})
                        history.append([checked_at, k, no, last, t, "" if gap is None else f"{gap:.1f}", "台が戻った・通知"])
                    else:
                        history.append([checked_at, k, no, last, t, "", "台の再掲載（更新なし）"])
                elif t != before:
                    gap = gap_hours(before, t)
                    notify = gap is not None and gap >= MIN_GAP_HOURS
                    if notify:
                        ups.append({"no": no, "before": before, "after": t, "gap": gap})
                    history.append([checked_at, k, no, before, t, "" if gap is None else f"{gap:.1f}", "更新・通知" if notify else "更新"])
            for no in old["m"]:
                if no not in now_m:
                    gone_m[no] = old["m"][no]
                    history.append([checked_at, k, no, old["m"][no], "", "", "台が消えた"])
            if ups:
                events.append({"kind": "update", "k": k, "machines": ups})
            if back:
                events.append({"kind": "machine_back", "k": k, "machines": back})
            if added:
                events.append({"kind": "added", "k": k, "machines": added})
            st[k] = {"m": now_m, "listed": True, "miss": 0, "q": qk}
            if gone_m:
                st[k]["gone_m"] = gone_m

        # この地域の店で、今回の一覧に出なかった店
        for k, v in st.items():
            if v.get("q") == qk and v.get("listed", True) and k not in keys_now:
                v["miss"] = v.get("miss", 0) + 1
                if v["miss"] >= GONE_AFTER_MISSES:
                    v["listed"] = False
                    v["gone_at"] = checked_at
                    history.append([checked_at, k, "", latest(v["m"].values()), "", "", "一覧から消えた"])
        if qk not in state["queries"]:
            state["queries"].append(qk)

    # 読めなかった回の数え方（失敗を「変化なし」とは言わない）
    if errors:
        if state["fail"] == 0:
            state["fail_since"] = checked_at
        state["fail"] += 1
        if state["fail"] == FAIL_REPORT_AT:
            events.append({"kind": "fail", "k": "", "since": short_time(state["fail_since"] + ":00")})
    else:
        if state["fail"] >= FAIL_REPORT_AT:
            events.append({"kind": "recovered", "k": ""})
        state["fail"] = 0
        state["fail_since"] = ""

    log(f"変化 {len(history)} 件、通知 {len(events)} 件" + ("（夜なのでためる）" if quiet else ""))

    # 夜はためておき、朝に送る
    sent = 0
    if quiet:
        state["pending"] += events
    else:
        held = state["pending"]
        state["pending"] = []
        lines = [describe(ev, names) for ev in held + events]
        lines = [x for x in lines if x]
        if lines:
            header = f"**公式更新ウォッチ（{now.strftime('%H:%M')}時点）**" + ("　※夜の間の分も含みます" if held else "")
            footer = DISCLAIMER if any(ev["kind"] not in ("fail", "recovered") for ev in held + events) else ""
            sent += send_chunks(header, lines, footer)
    # 見張りを加えたときと動作確認は、夜でもすぐ送る（手で操作したときなので）
    if info_lines:
        sent += send_chunks("**見張りの設定**", info_lines, "")
    if NOTIFY_TEST:
        sent += send_chunks(f"**動作確認（{now.strftime('%H:%M')}時点）**　今の最終更新", test_lines or ["（載っている店はありません）"], "")
    log(f"送った通知 {sent} 通")

    # 送れたあとで記録と状態を保存する（送信に失敗したら、次の回にもう一度判定して送る）
    write_history(history)
    save_state(state)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as e:
        log(str(e))
        sys.exit(1)

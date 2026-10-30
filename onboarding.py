"""
要員追加（新規参画者の権限パック）まわりの純粋ロジック

外部通信（Slack / Sheets / Vertex AI）は一切行わない。main.py のハンドラから呼ばれる。
設計の正本（work2026 側）: docs/01_pmo/2026-10-29_pmo_member-onboarding-automation-design.md
権限パック定義: member_packs.yaml（work2026 の config/sej_member_packs.yaml の写し）

既存の問合せ登録を壊さないための原則:
  - 要員追加 / 完了連絡は、1行目に専用キーワードがあるときだけ動く
  - 確認返信（OK）は、スレッド内にこの Bot のプレビューがあるときだけ動く
  - それ以外は従来の問合せ登録にそのまま流す
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

import yaml

PACKS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "member_packs.yaml")

ONBOARDING_KEYWORDS = ["【要員追加】", "[要員追加]", "要員追加：", "要員追加:"]
COMPLETION_KEYWORDS = ["【完了連絡】", "[完了連絡]", "完了連絡：", "完了連絡:"]

PREVIEW_MARKER = "[要員追加プレビュー]"
CONFIRM_WORDS = ("ok", "ｏｋ", "OK", "承認", "確定", "お願いします", "進めてください", "進めて")

TEAM_KEYWORDS = [
    ("infra", ["インフラ", "SRE", "infra"]),
    ("app", ["アプリ", "app"]),
    ("looker", ["Looker", "looker", "ルッカー"]),
]
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")


def load_packs(path: str = PACKS_PATH) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------- 判定

def is_onboarding_request(first_line: str) -> bool:
    return any(kw in first_line for kw in ONBOARDING_KEYWORDS)


def is_completion_request(first_line: str) -> bool:
    return any(kw in first_line for kw in COMPLETION_KEYWORDS)


def is_direct_confirm(text: str) -> bool:
    """【要員追加】の本文に「確定」だけの行があれば、プレビューを挟まず登録する（スレッド履歴の権限が無い環境向け）。"""
    return bool(re.search(r"^\s*(確定|登録実行)\s*$", text, re.MULTILINE))


def is_confirmation(text: str) -> bool:
    """短い「OK」系の返信か。長文は確認とみなさない（従来の問合せ登録に流す）。"""
    stripped = text.strip()
    if not stripped or len(stripped) > 80:
        return False
    first = stripped.splitlines()[0].strip()
    return any(first.startswith(w) for w in CONFIRM_WORDS)


# ---------------------------------------------------------------- 依頼の解析

def _labeled(text: str, labels: List[str]) -> Optional[str]:
    for label in labels:
        m = re.search(rf"(?:{label})\s*[:：]\s*(.+)", text)
        if m:
            return m.group(1).strip()
    return None


def detect_team(text: str) -> Optional[str]:
    labeled = _labeled(text, ["チーム", "所属", "領域"])
    target = labeled if labeled else text
    for team, words in TEAM_KEYWORDS:
        if any(w in target for w in words):
            return team
    return None


def parse_onboarding_request(text: str) -> Dict[str, Any]:
    """【要員追加】の本文から 氏名・メール・チーム・期日 を取り出す。足りないものは missing に入れる。"""
    email_match = EMAIL_RE.search(text)
    name = _labeled(text, ["氏名", "名前", "対象者", "対象"])
    if name:
        name = re.sub(r"\s*[（(].*?[）)]\s*$", "", name).strip()
        name = re.sub(r"さん$", "", name).strip()
        if EMAIL_RE.search(name):
            name = None
    person = {
        "name": name,
        "email": email_match.group(0) if email_match else None,
        "team": detect_team(text),
        "due": _labeled(text, ["期日", "期限", "希望日", "利用開始"]),
    }
    person["missing"] = [
        label for key, label in (("name", "氏名"), ("email", "メールアドレス"), ("team", "チーム（インフラ/アプリ/Looker）"))
        if not person[key]
    ]
    return person


def parse_exclusions(text: str) -> List[str]:
    """OK 返信内の「除く: A, B」から除外したい項目名を取り出す。"""
    value = _labeled(text, ["除く", "除外", "不要"])
    if not value:
        return []
    return [v.strip() for v in re.split(r"\s*[,、，/／]\s*", value) if v.strip()]


# ---------------------------------------------------------------- パック展開

def team_items(packs: Dict[str, Any], team: str) -> List[Dict[str, Any]]:
    spec = packs["teams"][team]
    exclude = set(spec.get("exclude", []))
    items: List[Dict[str, Any]] = []
    for pack_name in spec["packs"]:
        for item in packs["packs"].get(pack_name) or []:
            if item["id"] not in exclude:
                items.append(item)
    return items


def _as_text(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(value)
    return value or ""


def expand_ledger_entries(
    person: Dict[str, Any],
    packs: Dict[str, Any],
    exclusions: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """権限種別ごとに 1 件。write_to_spreadsheet が受け取る形（extract_info_with_gemini と同じキー）。"""
    exclusions = exclusions or []
    team_label = packs["teams"][person["team"]]["label"]
    entries: List[Dict[str, Any]] = []
    for item in team_items(packs, person["team"]):
        tag = item.get("ledger_tag")
        if not tag:
            continue
        if any(ex and (ex in tag or ex in item["title"] or ex == item["id"]) for ex in exclusions):
            continue
        detail = item["title"]
        if item.get("groups"):
            detail += "（" + ", ".join(item["groups"]) + "）"
        if item.get("depends_on"):
            detail += f" ※{item['depends_on']}の後"
        entries.append(
            {
                "target_name": person["name"],
                "target_email": person["email"],
                "tags": ["アカウント管理", "アカウント新規申請登録", tag, team_label, None],
                "details": f"【要員追加】{detail}",
                "due_date": person.get("due") or "",
                "order_number": "",
                "order_name": "",
            }
        )
    return entries


# ---------------------------------------------------------------- プレビュー / 確認

def build_preview_message(person: Dict[str, Any], entries: List[Dict[str, Any]], packs: Dict[str, Any]) -> str:
    team_label = packs["teams"][person["team"]]["label"]
    lines = [
        f"{PREVIEW_MARKER} 以下の内容で、権限ごとに問合せ台帳へ{len(entries)}件登録します。",
        f"対象者: {person['name']}（{person['email']}） / チーム: {team_label} / 期日: {person.get('due') or '未指定'}",
        "",
    ]
    for i, e in enumerate(entries, 1):
        lines.append(f"{i}. {e['tags'][2]}: {e['details'].replace('【要員追加】', '')}")
    lines += [
        "",
        "この内容でよければ、このスレッドで「OK」と返信してください（このBotへメンション）。",
        "外したい項目があれば「OK 除く: HCP Terraform」のように書いてください。内容を変える場合は、もう一度【要員追加】から送り直してください。",
        # 機械可読（確認返信時にスレッドから復元する）
        f"[meta] name={person['name']}|email={person['email']}|team={person['team']}|due={person.get('due') or ''}",
    ]
    return "\n".join(lines)


def parse_preview_meta(preview_text: str) -> Optional[Dict[str, Any]]:
    if PREVIEW_MARKER not in preview_text:
        return None
    m = re.search(r"\[meta\]\s*name=(.*?)\|email=(.*?)\|team=(.*?)\|due=(.*)", preview_text)
    if not m:
        return None
    name, email, team, due = (g.strip() for g in m.groups())
    if not (name and email and team):
        return None
    return {"name": name, "email": email, "team": team, "due": due, "missing": []}


def find_preview_in_thread(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """スレッドのメッセージ（古い順）から、最新のプレビューを探す。"""
    for msg in reversed(messages):
        meta = parse_preview_meta(msg.get("text", "") or "")
        if meta:
            return meta
    return None


# ---------------------------------------------------------------- 完了連絡

def parse_completion_numbers(text: str) -> List[int]:
    """【完了連絡】の本文から問合せ No を取り出す（例: 「問合せNo 101, 102-104」）。"""
    body = "\n".join(text.splitlines()[0:])
    body = re.sub(r"[【\[]完了連絡[】\]]|完了連絡[:：]", "", body)
    numbers: List[int] = []
    for m in re.finditer(r"(\d+)\s*[-〜~ー]\s*(\d+)|(\d+)", body):
        if m.group(1) and m.group(2):
            a, b = int(m.group(1)), int(m.group(2))
            if 0 < b - a <= 30:
                numbers.extend(range(a, b + 1))
        elif m.group(3):
            numbers.append(int(m.group(3)))
    seen, ordered = set(), []
    for n in numbers:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


SLACK_URL_RE = re.compile(r"/archives/([A-Z0-9]+)/p(\d{10})(\d{6})")


def parse_slack_url(url: str) -> Optional[Dict[str, str]]:
    m = SLACK_URL_RE.search(url or "")
    if not m:
        return None
    return {"channel": m.group(1), "thread_ts": f"{m.group(2)}.{m.group(3)}"}


def find_item_by_ledger_tag(packs: Dict[str, Any], tag: str) -> Optional[Dict[str, Any]]:
    for pack_items in packs["packs"].values():
        for item in pack_items or []:
            if item.get("ledger_tag") == tag:
                return item
    return None


def _norm(s: str) -> str:
    return re.sub(r"[\s\u3000._\-]+", "", s or "").lower()


def match_user_by_name(members: List[Dict[str, Any]], name: str, email: str = "") -> Optional[str]:
    """
    users:read.email が無い場合の代替。Slack ユーザー一覧から、氏名（またはメールのローカル部）で
    一意に特定できたときだけ User ID を返す。複数候補・該当なしは None（誤メンション防止）。
    """
    want_name = _norm(name)
    tokens = [_norm(t) for t in re.split(r"[._\-]", email.split("@")[0])] if email else []
    tokens = [t for t in tokens if t]
    hits = []
    for m in members:
        if m.get("deleted") or m.get("is_bot"):
            continue
        prof = m.get("profile", {}) or {}
        hay = [_norm(x) for x in (m.get("real_name"), m.get("name"), prof.get("real_name"), prof.get("display_name"))]
        hay = [h for h in hay if h]
        if want_name and any(want_name in h or (h and h in want_name and len(h) >= 2) for h in hay):
            hits.append(m["id"])
        elif len(tokens) >= 2 and any(all(t in h for t in tokens) for h in hay):
            hits.append(m["id"])
    unique = list(dict.fromkeys(hits))
    return unique[0] if len(unique) == 1 else None


def build_completion_message(row: List[str], packs: Dict[str, Any], mention: str) -> str:
    """台帳 1 行（問合せリストの A〜P）から、本人への確認依頼を作る。"""
    tag = row[7] if len(row) > 7 else ""
    item = find_item_by_ledger_tag(packs, tag)
    if not item:
        return f"{mention} 「{tag or '権限'}」の対応が完了しました。アクセスできるかご確認ください。"
    if item.get("self_check") is False:
        return (
            f"{mention} 「{tag}」の対応が完了しました。"
            "個別の確認は不要です（Google Cloud 環境にアクセスできるかの確認で見ます）。"
        )
    msg = (
        f"{mention} 「{tag}」の対応が完了しました。\n"
        f"確認: {item['title']}\n"
        "確認できたら、チェックリストの「確認済み」にチェックを入れてください。入れない場合は、このスレッドでお知らせください。"
    )
    if item.get("note"):
        msg += f"\n補足: {item['note']}"
    link = _as_text(item.get("link"))
    if link:
        msg += f"\nリンク:\n{link}"
    if item.get("secret"):
        msg += "\n※初期パスワード等は、このスレッドには書きません。別途DMでお渡しします。"
    return msg

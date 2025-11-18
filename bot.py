# bot.py — LoanLink
# 停止期間UI + 追加/削除 + 備考欄付き + Admin手動貸出
# + 貸出申請通知メンション + プロジェクト単位複数台申請

import os, json, base64
from datetime import datetime, timedelta, timezone, date
from typing import Optional, List, Tuple
import discord
from discord.ext import commands
from discord import ui
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from gspread_formatting import format_cell_range, CellFormat, TextFormat, Color, set_frozen
import re

# ========= 環境変数 =========
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SHEET_KEY = os.getenv("GOOGLE_SHEET_KEY")
SA_JSON_PATH = os.getenv("GOOGLE_SA_JSON_PATH")
SA_JSON_B64 = os.getenv("GOOGLE_SA_JSON_B64")
ADMIN_ROLE_NAME = os.getenv("ADMIN_ROLE_NAME")

# ========= Google 認証 =========
def get_gspread_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    if SA_JSON_B64:
        info = json.loads(base64.b64decode(SA_JSON_B64).decode("utf-8"))
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    elif SA_JSON_PATH:
        creds = Credentials.from_service_account_file(SA_JSON_PATH, scopes=scopes)
    else:
        raise RuntimeError("サービスアカウント情報が見つかりません。")
    return gspread.authorize(creds)

gc = get_gspread_client()
sh = gc.open_by_key(SHEET_KEY)

# ========= 定数 =========
CAMPUS_CHOICES = ["小白川キャンパス", "飯田キャンパス", "米沢キャンパス", "鶴岡キャンパス", "その他"]

REQ_HEADERS = [
    "記録時刻", "ユーザーID", "ユーザー名", "所属キャンパス",
    "操作", "機材ID", "機材名", "返却予定日", "用途/状態", "コメント", "申請ステータス"
]
INV_HEADERS = [
    "機材ID", "機材名", "カテゴリ", "備考",
    "ステータス", "借用者", "返却予定日"
]
CFG_HEADERS = ["キー", "値"]
BLK_HEADERS = ["種別", "名前", "開始", "終了", "モード", "有効"]  # 種別, 名前, 開始, 終了, モード(recurring/once), 有効(TRUE/FALSE)
PROJ_HEADERS = ["プロジェクト名", "説明"]

def get_or_create_ws(title: str, headers: List[str]):
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=20)
    cur = ws.row_values(1)
    if not cur:
        ws.append_row(headers)
    else:
        if cur != headers:
            end_col = chr(ord("A") + len(headers) - 1)
            ws.update([headers], f"A1:{end_col}1")
    return ws

req_ws = get_or_create_ws("requests", REQ_HEADERS)
inv_ws = get_or_create_ws("inventory", INV_HEADERS)
cfg_ws = get_or_create_ws("config", CFG_HEADERS)
blk_ws = get_or_create_ws("blackouts", BLK_HEADERS)
proj_ws = get_or_create_ws("projects", PROJ_HEADERS)

def style_headers(ws, headers: List[str]):
    end_col = chr(ord("A") + len(headers) - 1)
    ws.update([headers], f"A1:{end_col}1")
    set_frozen(ws, rows=1)
    format_cell_range(ws, f"A1:{end_col}1", CellFormat(
        backgroundColor=Color(0.90, 0.95, 1.00),
        textFormat=TextFormat(bold=True),
    ))

style_headers(req_ws, REQ_HEADERS)
style_headers(inv_ws, INV_HEADERS)
style_headers(cfg_ws, CFG_HEADERS)
style_headers(blk_ws, BLK_HEADERS)
style_headers(proj_ws, PROJ_HEADERS)

# ========= 日付ユーティリティ =========
JST = timezone(timedelta(hours=9))

def now_jst_str() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S JST")

def today_jst() -> date:
    return datetime.now(JST).date()

def parse_md(md: str) -> Tuple[int, int]:
    m, d = map(int, md.split("-"))
    return m, d

def within_md(y: int, m: int, d: int, start_md: str, end_md: str) -> bool:
    sm, sd = parse_md(start_md)
    em, ed = parse_md(end_md)
    start = date(y, sm, sd)
    end = date(y, em, ed)
    return start <= date(y, m, d) <= end

# ========= config / blackout シート =========
def cfg_get(key: str) -> Optional[str]:
    vals = cfg_ws.get_all_values()
    for r in vals[1:]:
        if r and r[0] == key:
            return r[1] if len(r) > 1 else ""
    return None

def cfg_set(key: str, value: str):
    vals = cfg_ws.get_all_values()
    for i, r in enumerate(vals[1:], start=2):
        if r and r[0] == key:
            cfg_ws.update_cell(i, 2, str(value))
            return
    cfg_ws.append_row([key, str(value)])

def blk_list() -> List[dict]:
    vals = blk_ws.get_all_values()
    res = []
    for r in vals[1:]:
        if not r:
            continue
        t = (r[0] if len(r) > 0 else "").strip()
        name = (r[1] if len(r) > 1 else "").strip()
        start = (r[2] if len(r) > 2 else "").strip()
        end = (r[3] if len(r) > 3 else "").strip()
        mode = (r[4] if len(r) > 4 else "").strip()
        active = (r[5] if len(r) > 5 else "TRUE").strip().upper() in ["TRUE", "1", "YES", "ON"]
        res.append({"種別": t, "名前": name, "開始": start, "終了": end, "モード": mode, "有効": active})
    return res

def blk_add(t: str, name: str, start: str, end: str, mode: str, active: bool = True):
    blk_ws.append_row([t, name, start, end, mode, "TRUE" if active else "FALSE"])

def blk_toggle(name: str, active: bool) -> bool:
    vals = blk_ws.get_all_values()
    for i, r in enumerate(vals[1:], start=2):
        if len(r) > 1 and r[1] == name:
            blk_ws.update_cell(i, 6, "TRUE" if active else "FALSE")
            return True
    return False

def blk_delete(name: str) -> bool:
    vals = blk_ws.get_all_values()
    for i, r in enumerate(vals[1:], start=2):
        if len(r) > 1 and r[1] == name:
            blk_ws.delete_rows(i)
            return True
    return False

def human_period(b: dict) -> str:
    if b["モード"] == "recurring":
        return f"{b['開始']}〜{b['終了']}（毎年）"
    return f"{b['開始']}〜{b['終了']}"

def calc_is_blackout(today: Optional[date] = None) -> Tuple[bool, str, str]:
    if today is None:
        today = today_jst()
    y, m, d = today.year, today.month, today.day
    for b in blk_list():
        if not b["有効"]:
            continue
        if b["種別"] in ["festival", "recruit"] and b["モード"] == "recurring":
            if within_md(y, m, d, b["開始"], b["終了"]):
                label = "文化祭" if b["種別"] == "festival" else "新歓"
                return True, label, f"{b['開始']}〜{b['終了']}"
        elif b["種別"] == "custom" and b["モード"] == "once":
            try:
                s = date.fromisoformat(b["開始"])
                e = date.fromisoformat(b["終了"])
                if s <= today <= e:
                    return True, b["名前"] or "運営都合", f"{b['開始']}〜{b['終了']}"
            except Exception:
                continue
    return False, "", ""

# ========= 共通ユーティリティ =========
def is_admin(member: discord.Member) -> bool:
    if ADMIN_ROLE_NAME and any(r.name == ADMIN_ROLE_NAME for r in member.roles):
        return True
    return member.guild_permissions.administrator

def inv_all() -> List[dict]:
    vals = inv_ws.get_all_values()
    if len(vals) < 2:
        return []
    res = []
    for r in vals[1:]:
        padded = (r + [""] * len(INV_HEADERS))[:len(INV_HEADERS)]
        res.append({
            "機材ID": padded[0],
            "機材名": padded[1],
            "カテゴリ": padded[2],
            "備考": padded[3],
            "ステータス": padded[4],
            "借用者": padded[5],
            "返却予定日": padded[6],
        })
    return res

def inv_categories() -> List[str]:
    return sorted(set(r["カテゴリ"] for r in inv_all() if r["カテゴリ"]))

def inv_find_row(item_id: str) -> Optional[int]:
    col = inv_ws.col_values(1)
    try:
        return col.index(item_id) + 1
    except ValueError:
        return None

def inv_available(cat: str) -> List[dict]:
    return [
        r for r in inv_all()
        if r["カテゴリ"] == cat and (r["ステータス"] in ["貸出可", ""] or r["ステータス"] is None)
    ]

def inv_borrowed_by(user_name: str) -> List[dict]:
    return [
        r for r in inv_all()
        if r["借用者"] == user_name and r["ステータス"] in ["貸出中", "貸出申請中"]
    ]

def make_prefix(category: str) -> str:
    p = "".join(ch for ch in category if ch.isalnum()).upper()
    return p[:8] if p else "CAT"

def generate_item_id(category: str) -> str:
    pref = make_prefix(category)
    existing = inv_ws.col_values(1)[1:]
    max_n = 0
    for s in existing:
        if s.startswith(pref + "-") and s[len(pref) + 1:].isdigit():
            max_n = max(max_n, int(s[len(pref) + 1:]))
    return f"{pref}-{max_n + 1:03d}"

def proj_all() -> List[dict]:
    """projects シートからプロジェクト一覧を取得"""
    vals = proj_ws.get_all_values()
    if len(vals) < 2:
        return []
    res = []
    for r in vals[1:]:
        name = (r[0].strip() if len(r) > 0 else "")
        desc = (r[1].strip() if len(r) > 1 else "")
        if name:
            res.append({"name": name, "desc": desc})
    return res

async def maybe_announce(current_channel: discord.abc.Messageable, text: str):
    ch_id = cfg_get("ANNOUNCE_CHANNEL_ID")
    if isinstance(current_channel, discord.Interaction):
        guild = current_channel.guild
    else:
        guild = getattr(current_channel, "guild", None)

    if ch_id and guild:
        try:
            ch = guild.get_channel(int(ch_id))
            if ch:
                await ch.send(f"📢 {text}")
                return
        except Exception:
            pass
    # fallback
    if isinstance(current_channel, discord.Interaction):
        await current_channel.channel.send(f"📢 {text}")
    else:
        await current_channel.send(f"📢 {text}")

# ★ 貸出申請用 通知ヘルパー（メンション先は config の LOAN_NOTIFY_TARGET）
async def notify_request(source, text: str):
    """
    LOAN_NOTIFY_TARGET に設定された
      - role:<id>
      - user:<id>
    を元にメンションを付けて ANNOUNCE_CHANNEL_ID へ送信。
    無ければ現在のチャンネルにそのまま送信。
    """
    guild = None
    channel = None
    if isinstance(source, discord.Interaction):
        guild = source.guild
        channel = source.channel
    elif isinstance(source, discord.Message):
        guild = source.guild
        channel = source.channel

    mention = ""
    target = cfg_get("LOAN_NOTIFY_TARGET")
    if target and guild:
        kind, _, id_str = target.partition(":")
        try:
            target_id = int(id_str)
        except Exception:
            target_id = None
        if target_id is not None:
            if kind == "role":
                role = guild.get_role(target_id)
                if role:
                    mention = role.mention
            elif kind == "user":
                member = guild.get_member(target_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(target_id)
                    except Exception:
                        member = None
                if member:
                    mention = member.mention

    # 送信先チャンネル（admin用に ANNOUNCE_CHANNEL_ID を優先）
    ch_id = cfg_get("ANNOUNCE_CHANNEL_ID")
    if guild and ch_id:
        c = guild.get_channel(int(ch_id))
        if c:
            channel = c

    if channel:
        msg = f"{mention} {text}" if mention else text
        await channel.send(msg)

# ========= Discord Bot =========
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # メンバー取得に必要
bot = commands.Bot(command_prefix="!", intents=intents)

# ========= 停止期間 Admin UI =========
class BlackoutAdminView(ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(SetFestivalButton())
        self.add_item(SetRecruitButton())
        self.add_item(AddCustomBlackoutButton())
        self.add_item(ToggleCustomBlackoutButton())
        self.add_item(DeleteBlackoutButton())
        self.add_item(SetAnnounceHereButton())
        self.add_item(ListBlackoutsButton())

class SetFestivalButton(ui.Button):
    def __init__(self):
        super().__init__(label="文化祭 期間を設定", style=discord.ButtonStyle.primary, custom_id="blk_set_fes")

    async def callback(self, itx: discord.Interaction):
        if not is_admin(itx.user):
            return await itx.response.send_message("権限がありません。", ephemeral=True)
        await itx.response.send_modal(FestivalModal())

class SetRecruitButton(ui.Button):
    def __init__(self):
        super().__init__(label="新歓 期間を設定", style=discord.ButtonStyle.primary, custom_id="blk_set_rec")

    async def callback(self, itx: discord.Interaction):
        if not is_admin(itx.user):
            return await itx.response.send_message("権限がありません。", ephemeral=True)
        await itx.response.send_modal(RecruitModal())

class AddCustomBlackoutButton(ui.Button):
    def __init__(self):
        super().__init__(label="カスタム停止 追加", style=discord.ButtonStyle.success, custom_id="blk_add_custom")

    async def callback(self, itx: discord.Interaction):
        if not is_admin(itx.user):
            return await itx.response.send_message("権限がありません。", ephemeral=True)
        await itx.response.send_modal(AddCustomModal())

class ToggleCustomBlackoutButton(ui.Button):
    def __init__(self):
        super().__init__(label="カスタム停止 有効/無効", style=discord.ButtonStyle.secondary, custom_id="blk_toggle_custom")

    async def callback(self, itx: discord.Interaction):
        if not is_admin(itx.user):
            return await itx.response.send_message("権限がありません。", ephemeral=True)
        customs = [b for b in blk_list() if b["種別"] == "custom"]
        if not customs:
            return await itx.response.send_message("カスタム停止は未登録です。", ephemeral=True)
        opts = [
            discord.SelectOption(
                label=f"{b['名前']}（{human_period(b)}）{'✅' if b['有効'] else '⛔'}",
                value=b["名前"],
            )
            for b in customs[:25]
        ]
        view = ui.View(timeout=60)
        view.add_item(ToggleCustomSelect(opts))
        await itx.response.send_message("有効/無効を切り替える項目を選択：", view=view, ephemeral=True)

class ToggleCustomSelect(ui.Select):
    def __init__(self, opts):
        super().__init__(placeholder="カスタム停止を選択", options=opts, custom_id="blk_toggle_sel")

    async def callback(self, itx: discord.Interaction):
        name = self.values[0]
        items = [b for b in blk_list() if b["名前"] == name]
        if not items:
            return await itx.response.send_message("対象が見つかりませんでした。", ephemeral=True)
        new_state = not items[0]["有効"]
        blk_toggle(name, new_state)
        await itx.response.send_message(f"「{name}」を{'有効化' if new_state else '無効化'}しました。", ephemeral=True)
        await maybe_announce(itx, f"停止期間「{name}」を{'有効化' if new_state else '無効化'}しました。")

class DeleteBlackoutButton(ui.Button):
    def __init__(self):
        super().__init__(label="停止期間を削除", style=discord.ButtonStyle.danger, custom_id="blk_delete")

    async def callback(self, itx: discord.Interaction):
        if not is_admin(itx.user):
            return await itx.response.send_message("権限がありません。", ephemeral=True)
        items = blk_list()
        if not items:
            return await itx.response.send_message("停止期間は未設定です。", ephemeral=True)
        opts = [
            discord.SelectOption(
                label=f"[{b['種別']}] {b['名前']}（{human_period(b)}）",
                value=b["名前"],
            )
            for b in items[:25]
        ]
        view = ui.View(timeout=60)
        view.add_item(DeleteBlackoutSelect(opts))
        await itx.response.send_message("削除する停止期間を選択：", view=view, ephemeral=True)

class DeleteBlackoutSelect(ui.Select):
    def __init__(self, opts):
        super().__init__(placeholder="停止期間を選択", options=opts, custom_id="blk_delete_sel")

    async def callback(self, itx: discord.Interaction):
        name = self.values[0]
        ok = blk_delete(name)
        if ok:
            await itx.response.send_message(f"停止期間「{name}」を削除しました。", ephemeral=True)
            await maybe_announce(itx, f"停止期間「{name}」を削除しました。")
        else:
            await itx.response.send_message("削除対象が見つかりませんでした。", ephemeral=True)

class SetAnnounceHereButton(ui.Button):
    def __init__(self):
        super().__init__(label="お知らせチャンネルをここに設定", style=discord.ButtonStyle.secondary, custom_id="blk_set_announce")

    async def callback(self, itx: discord.Interaction):
        if not is_admin(itx.user):
            return await itx.response.send_message("権限がありません。", ephemeral=True)
        cfg_set("ANNOUNCE_CHANNEL_ID", str(itx.channel.id))
        await itx.response.send_message("このチャンネルをお知らせ先に設定しました。", ephemeral=True)

class ListBlackoutsButton(ui.Button):
    def __init__(self):
        super().__init__(label="現在の停止設定を表示", style=discord.ButtonStyle.secondary, custom_id="blk_list")

    async def callback(self, itx: discord.Interaction):
        blks = blk_list()
        if not blks:
            return await itx.response.send_message("停止期間は未設定です。", ephemeral=True)
        lines = ["**停止期間一覧**"]
        for b in blks:
            mk = "✅" if b["有効"] else "⛔"
            lines.append(f"- {mk} [{b['種別']}] {b['名前'] or '(無題)'}: {human_period(b)}")
        await itx.response.send_message("\n".join(lines), ephemeral=True)

class FestivalModal(ui.Modal, title="文化祭 期間設定（毎年）"):
    start = ui.TextInput(label="開始（MM-DD）", placeholder="例: 09-20", required=True, max_length=5)
    end = ui.TextInput(label="終了（MM-DD）", placeholder="例: 11-05", required=True, max_length=5)

    async def on_submit(self, itx: discord.Interaction):
        for b in blk_list():
            if b["種別"] == "festival":
                blk_toggle(b["名前"], False)
        blk_add("festival", "文化祭", str(self.start), str(self.end), "recurring", True)
        await itx.response.send_message(f"文化祭: {self.start}〜{self.end} を設定しました。", ephemeral=True)
        await maybe_announce(itx, f"文化祭期間を **{self.start}〜{self.end}** に設定しました。")

class RecruitModal(ui.Modal, title="新歓 期間設定（毎年）"):
    start = ui.TextInput(label="開始（MM-DD）", placeholder="例: 04-01", required=True, max_length=5)
    end = ui.TextInput(label="終了（MM-DD）", placeholder="例: 05-15", required=True, max_length=5)

    async def on_submit(self, itx: discord.Interaction):
        for b in blk_list():
            if b["種別"] == "recruit":
                blk_toggle(b["名前"], False)
        blk_add("recruit", "新歓", str(self.start), str(self.end), "recurring", True)
        await itx.response.send_message(f"新歓: {self.start}〜{self.end} を設定しました。", ephemeral=True)
        await maybe_announce(itx, f"新歓期間を **{self.start}〜{self.end}** に設定しました。")

class AddCustomModal(ui.Modal, title="カスタム停止 追加（単発）"):
    name = ui.TextInput(label="名前", placeholder="例: 学内イベント対応", required=True, max_length=50)
    start = ui.TextInput(label="開始（YYYY-MM-DD）", placeholder="例: 2025-10-25", required=True, max_length=10)
    end = ui.TextInput(label="終了（YYYY-MM-DD）", placeholder="例: 2025-10-28", required=True, max_length=10)

    async def on_submit(self, itx: discord.Interaction):
        blk_add("custom", str(self.name), str(self.start), str(self.end), "once", True)
        await itx.response.send_message(
            f"カスタム停止を追加: {self.name} / {self.start}〜{self.end}",
            ephemeral=True,
        )
        await maybe_announce(itx, f"カスタム停止 **{self.name}** を {self.start}〜{self.end} で有効化しました。")

# ========= Admin メニュー =========
class AdminPanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(RegisterItemButton())
        self.add_item(AdminInventoryListButton())
        self.add_item(AdminRequestsPeekButton())
        self.add_item(AdminApproveLoansButton())
        self.add_item(AdminApproveReturnsButton())
        self.add_item(AdminManualLoanButton())          # 手動貸出
        self.add_item(SetLoanNotifyTargetButton())      # 貸出通知メンション設定
        self.add_item(OpenBlackoutAdminButton())

class OpenBlackoutAdminButton(ui.Button):
    def __init__(self):
        super().__init__(label="停止期間の設定", style=discord.ButtonStyle.danger, custom_id="open_blk_admin")

    async def callback(self, itx: discord.Interaction):
        if not is_admin(itx.user):
            return await itx.response.send_message("権限がありません。", ephemeral=True)
        await itx.response.send_message("停止期間の設定", view=BlackoutAdminView(), ephemeral=True)

# ★ 貸出通知のメンション先設定ボタン & モーダル
class SetLoanNotifyTargetButton(ui.Button):
    def __init__(self):
        super().__init__(label="🔔 貸出通知のメンション先を設定", style=discord.ButtonStyle.secondary, custom_id="admin_set_notify")

    async def callback(self, itx: discord.Interaction):
        if not is_admin(itx.user):
            return await itx.response.send_message("権限がありません。", ephemeral=True)
        await itx.response.send_modal(SetLoanNotifyTargetModal())

class SetLoanNotifyTargetModal(ui.Modal, title="貸出通知のメンション先を設定"):
    target = ui.TextInput(
        label="メンションまたはID",
        placeholder="例: @機材管理ロール / @ユーザー / 123456789012345678",
        required=True,
        max_length=64,
    )

    async def on_submit(self, itx: discord.Interaction):
        guild = itx.guild
        if guild is None:
            return await itx.response.send_message("サーバー内でのみ使用できます。", ephemeral=True)

        raw = self.target.value.strip()

        # ロールメンション <@&id>
        m_role = re.fullmatch(r"<@&(\d+)>", raw)
        # ユーザーメンション <@id> or <@!id>
        m_user = re.fullmatch(r"<@!?(\d+)>", raw)

        # 1) ロールメンション優先
        if m_role:
            target_id = int(m_role.group(1))
            role = guild.get_role(target_id)
            if not role:
                return await itx.response.send_message("そのロールはサーバー内に見つかりません。", ephemeral=True)
            cfg_set("LOAN_NOTIFY_TARGET", f"role:{target_id}")
            return await itx.response.send_message(
                f"今後の貸出申請通知はロール {role.mention} をメンションします。",
                ephemeral=True,
            )

        # 2) ユーザーメンション
        if m_user:
            target_id = int(m_user.group(1))
            member = guild.get_member(target_id)
            if member is None:
                try:
                    member = await guild.fetch_member(target_id)
                except Exception:
                    member = None
            if not member:
                return await itx.response.send_message("そのユーザーはサーバー内に見つかりません。", ephemeral=True)
            cfg_set("LOAN_NOTIFY_TARGET", f"user:{target_id}")
            return await itx.response.send_message(
                f"今後の貸出申請通知は {member.mention} をメンションします。",
                ephemeral=True,
            )

        # 3) 数字だけなら ID として解釈（ロール → ユーザー の順）
        if raw.isdigit():
            target_id = int(raw)
            role = guild.get_role(target_id)
            if role:
                cfg_set("LOAN_NOTIFY_TARGET", f"role:{target_id}")
                return await itx.response.send_message(
                    f"今後の貸出申請通知はロール {role.mention} をメンションします。",
                    ephemeral=True,
                )
            member = guild.get_member(target_id)
            if member is None:
                try:
                    member = await guild.fetch_member(target_id)
                except Exception:
                    member = None
            if member:
                cfg_set("LOAN_NOTIFY_TARGET", f"user:{target_id}")
                return await itx.response.send_message(
                    f"今後の貸出申請通知は {member.mention} をメンションします。",
                    ephemeral=True,
                )
            return await itx.response.send_message("そのIDのロール/ユーザーは見つかりませんでした。", ephemeral=True)

        # 4) 名前でロール検索
        for r in guild.roles:
            if r.name == raw:
                cfg_set("LOAN_NOTIFY_TARGET", f"role:{r.id}")
                return await itx.response.send_message(
                    f"今後の貸出申請通知はロール {r.mention} をメンションします。",
                    ephemeral=True,
                )

        # 5) 名前でユーザー検索
        lower = raw.lower()
        member = None
        for m_ in guild.members:
            if m_.display_name.lower() == lower or m_.name.lower() == lower:
                member = m_
                break
        if member:
            cfg_set("LOAN_NOTIFY_TARGET", f"user:{member.id}")
            return await itx.response.send_message(
                f"今後の貸出申請通知は {member.mention} をメンションします。",
                ephemeral=True,
            )

        await itx.response.send_message(
            "ロール/ユーザーが見つかりませんでした。\n"
            "・ロールメンション（@ロール）\n"
            "・ユーザーメンション（@ユーザー）\n"
            "・ID（数値）\n"
            "・名前（ロール名 or ユーザーの表示名/ユーザー名）\n"
            "のいずれかで入力してください。",
            ephemeral=True,
        )

# ---- 機材登録（備考あり） ----
class RegisterItemButton(ui.Button):
    def __init__(self):
        super().__init__(label="機材登録（Admin）", style=discord.ButtonStyle.primary, custom_id="admin_register")

    async def callback(self, itx: discord.Interaction):
        if not is_admin(itx.user):
            return await itx.response.send_message("権限がありません。", ephemeral=True)
        cats = inv_categories()
        opts = [discord.SelectOption(label=c, value=c) for c in cats[:24]]
        opts.insert(0, discord.SelectOption(label="＋新規カテゴリ", value="__NEW__"))
        view = ui.View(timeout=60)
        view.add_item(RegisterCategorySelect(opts))
        await itx.response.send_message("カテゴリを選択：", view=view, ephemeral=True)

class RegisterCategorySelect(ui.Select):
    def __init__(self, opts):
        super().__init__(placeholder="カテゴリを選択", options=opts, custom_id="admin_sel_cat")

    async def callback(self, itx: discord.Interaction):
        if self.values[0] == "__NEW__":
            await itx.response.send_modal(RegisterItemModalNewCat())
        else:
            await itx.response.send_modal(RegisterItemModalExist(self.values[0]))

class RegisterItemModalExist(ui.Modal, title="機材登録（既存カテゴリ）"):
    def __init__(self, cat: str):
        super().__init__()
        self.cat = cat
        self.name = ui.TextInput(label="機材名", placeholder="例: Meta Quest 3 / MacBook Air M3", required=True)
        self.note = ui.TextInput(label="備考（任意）", placeholder="例: 付属品 /注意事項など", required=False)
        self.add_item(self.name)
        self.add_item(self.note)

    async def on_submit(self, itx: discord.Interaction):
        cid = generate_item_id(self.cat)
        inv_ws.append_row([cid, self.name.value, self.cat, self.note.value, "貸出可", "", ""])
        await itx.response.send_message(
            f"登録完了: {cid} / {self.name.value}\n備考: {self.note.value or '（なし）'}",
            ephemeral=True,
        )

class RegisterItemModalNewCat(ui.Modal, title="機材登録（新規カテゴリ）"):
    cat = ui.TextInput(label="カテゴリ名", placeholder="例: HMD / ノートPC / コントローラ", required=True)
    name = ui.TextInput(label="機材名",   placeholder="例: Meta Quest 3 / ThinkPad X1 Carbon", required=True)
    note = ui.TextInput(label="備考（任意）", placeholder="例: 付属品 /注意事項など", required=False)

    async def on_submit(self, itx: discord.Interaction):
        cid = generate_item_id(self.cat.value)
        inv_ws.append_row([cid, self.name.value, self.cat.value, self.note.value, "貸出可", "", ""])
        await itx.response.send_message(
            f"登録完了: {cid} / {self.name.value}\n備考: {self.note.value or '（なし）'}",
            ephemeral=True,
        )

class AdminInventoryListButton(ui.Button):
    def __init__(self):
        super().__init__(label="在庫一覧", style=discord.ButtonStyle.secondary, custom_id="admin_list")

    async def callback(self, itx: discord.Interaction):
        recs = inv_all()
        if not recs:
            return await itx.response.send_message("在庫なし。", ephemeral=True)
        st = {}
        for r in recs:
            key = r["ステータス"] or "不明"
            st[key] = st.get(key, 0) + 1
        msg = "**在庫状況**\n" + "\n".join(f"- {k}: {v}" for k, v in st.items())
        await itx.response.send_message(msg, ephemeral=True)

class AdminRequestsPeekButton(ui.Button):
    def __init__(self):
        super().__init__(label="直近申請ログ", style=discord.ButtonStyle.secondary, custom_id="admin_logs")

    async def callback(self, itx: discord.Interaction):
        vals = req_ws.get_all_values()
        if len(vals) < 2:
            return await itx.response.send_message("申請ログなし。", ephemeral=True)
        h = vals[0]
        data = vals[-10:]
        idx = {x: i for i, x in enumerate(h)}

        def g(r, k):
            return r[idx[k]] if k in idx and idx[k] < len(r) else ""

        lines = [
            "📜 **直近申請ログ（最大10件）**",
            "記録時刻 / ユーザー名 / 所属キャンパス / 操作 / 機材ID 機材名 / 状態",
        ]
        for r in data:
            lines.append(
                f"- {g(r, '記録時刻')} / {g(r, 'ユーザー名')} / {g(r, '所属キャンパス')} / "
                f"{g(r, '操作')} / {g(r, '機材ID')} {g(r, '機材名')} / {g(r, '申請ステータス')}"
            )
        await itx.response.send_message("\n".join(lines), ephemeral=True)

# ========= Admin 手動貸出 =========
class AdminManualLoanButton(ui.Button):
    def __init__(self):
        super().__init__(label="手動で貸出中にする", style=discord.ButtonStyle.secondary, custom_id="admin_manual_loan")

    async def callback(self, itx: discord.Interaction):
        if not is_admin(itx.user):
            return await itx.response.send_message("権限がありません。", ephemeral=True)
        items = inv_all()
        if not items:
            return await itx.response.send_message("在庫がありません。", ephemeral=True)
        candidates = [i for i in items if i["ステータス"] != "貸出中"]
        if not candidates:
            return await itx.response.send_message("貸出可能または申請中でない機材がありません。", ephemeral=True)
        view = ui.View(timeout=60)
        view.add_item(AdminManualItemSelect(candidates))
        await itx.response.send_message("貸出中にしたい機材を選択してください：", view=view, ephemeral=True)

class AdminManualItemSelect(ui.Select):
    def __init__(self, items: List[dict]):
        self.items = items
        opts = []
        for i in items[:25]:
            label = f"{i['機材名']} ({i['機材ID']})"
            desc = f"カテゴリ:{i['カテゴリ']} / 現ステータス:{i['ステータス'] or '-'}"
            opts.append(discord.SelectOption(label=label[:100], value=i["機材ID"], description=desc[:100]))
        super().__init__(placeholder="機材を選択", options=opts, custom_id="admin_manual_item")

    async def callback(self, itx: discord.Interaction):
        item_id = self.values[0]
        await itx.response.send_modal(AdminManualLoanModal(item_id))

class AdminManualLoanModal(ui.Modal, title="手動貸出登録"):
    def __init__(self, item_id: str):
        super().__init__()
        self.item_id = item_id
        self.borrower = ui.TextInput(
            label="貸出者（必須：メンション / ユーザーID / 表示名）",
            placeholder="例: @まーしゅ / 123456789012345678 / まーしゅ",
            required=True,
            max_length=50,
        )
        self.due = ui.TextInput(
            label="返却予定日（任意・YYYY-MM-DD）",
            placeholder="例: 2025-11-15",
            required=False,
            max_length=10,
        )
        self.note = ui.TextInput(
            label="用途/メモ（任意）",
            placeholder="例: 文化祭展示用 / 研究用途 など",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=300,
        )
        self.add_item(self.borrower)
        self.add_item(self.due)
        self.add_item(self.note)

    async def on_submit(self, itx: discord.Interaction):
        guild = itx.guild
        if guild is None:
            await itx.response.send_message("サーバー内でのみ使用できます。", ephemeral=True)
            return

        raw = self.borrower.value.strip()
        member: Optional[discord.Member] = None

        # 1) メンション形式 <@123> / <@!123>
        m = re.fullmatch(r"<@!?(\d+)>", raw)
        user_id: Optional[int] = None
        if m:
            user_id = int(m.group(1))

        # 2) 数字だけならユーザーIDとして扱う
        if user_id is None and raw.isdigit():
            user_id = int(raw)

        # user_id が取れた場合は get_member → fetch_member の順で試す
        if user_id is not None:
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except Exception:
                    member = None

        # 3) user_id 取れなかった場合は、表示名 / ユーザー名で検索（完全一致・小文字比較）
        if member is None and user_id is None:
            lower = raw.lower()
            for m_ in guild.members:
                if m_.display_name.lower() == lower or m_.name.lower() == lower:
                    member = m_
                    break

        # 見つからなかった
        if member is None:
            await itx.response.send_message(
                "サーバー内にそのユーザーが見つかりませんでした。\n"
                "・メンション（@ユーザー）\n"
                "・ユーザーID\n"
                "・表示名 / ユーザー名（完全一致）\n"
                "のいずれかで入力してください。\n\n"
                "※ できれば **メンション か ユーザーID** を使うのがおすすめです。",
                ephemeral=True,
            )
            return

        # ここから実際の登録処理
        admin_user = itx.user
        idx = inv_find_row(self.item_id)
        if idx is None:
            await itx.response.send_message("inventory に対象機材が見つかりませんでした。", ephemeral=True)
            return

        row = inv_ws.row_values(idx)
        inv_name = row[1] if len(row) > 1 else ""

        # inventory を「貸出中」に更新
        inv_ws.update_cell(idx, 5, "貸出中")               # ステータス
        inv_ws.update_cell(idx, 6, member.display_name)    # 借用者（表示名）
        inv_ws.update_cell(idx, 7, self.due.value.strip()) # 返却予定日

        # requests にも「借りる人」をユーザーとして記録
        req_ws.append_row([
            now_jst_str(),
            str(member.id),                 # ユーザーID = 借りる人
            member.display_name,            # ユーザー名 = 借りる人
            "未設定(管理)",                 # 所属キャンパス（手動なので不明）
            "貸出(管理)",                   # 操作
            self.item_id,
            inv_name,
            self.due.value.strip(),
            self.note.value.strip(),        # 用途/状態
            f"Admin {admin_user.display_name} が手動登録",  # コメント
            "approved",
        ])

        await itx.response.send_message(
            f"手動で貸出登録しました。\n"
            f"- 機材: {self.item_id} {inv_name}\n"
            f"- 貸出者: {member.display_name} (ID: {member.id})\n"
            f"- 返却予定日: {self.due.value or '未入力'}",
            ephemeral=True,
        )

# ========= 承認フロー =========
def req_pending(op: str) -> List[Tuple[int, List[str]]]:
    vals = req_ws.get_all_values()
    if len(vals) < 2:
        return []
    h = vals[0]
    idx = {x: i for i, x in enumerate(h)}
    out = []
    for i, r in enumerate(vals[1:], start=2):
        opv = r[idx.get("操作", -1)] if idx.get("操作") is not None and idx["操作"] < len(r) else ""
        st = r[idx.get("申請ステータス", -1)] if idx.get("申請ステータス") is not None and idx["申請ステータス"] < len(r) else ""
        if opv == op and st == "submitted":
            out.append((i, r))
    return out

class AdminApproveLoansButton(ui.Button):
    def __init__(self):
        super().__init__(label="貸出の承認/却下", style=discord.ButtonStyle.success, custom_id="admin_appr_loan")

    async def callback(self, itx: discord.Interaction):
        if not is_admin(itx.user):
            return await itx.response.send_message("権限がありません。", ephemeral=True)
        p = req_pending("貸出申請")
        if not p:
            return await itx.response.send_message("承認待ちの『貸出申請』はありません。", ephemeral=True)
        view = ui.View(timeout=60)
        view.add_item(PendingSelect("貸出申請", p))
        await itx.response.send_message("承認・却下する申請を選択：", view=view, ephemeral=True)

class AdminApproveReturnsButton(ui.Button):
    def __init__(self):
        super().__init__(label="返却の承認/却下", style=discord.ButtonStyle.primary, custom_id="admin_appr_ret")

    async def callback(self, itx: discord.Interaction):
        if not is_admin(itx.user):
            return await itx.response.send_message("権限がありません。", ephemeral=True)
        p = req_pending("返却申請")
        if not p:
            return await itx.response.send_message("承認待ちの『返却申請』はありません。", ephemeral=True)
        view = ui.View(timeout=60)
        view.add_item(PendingSelect("返却申請", p))
        await itx.response.send_message("承認・却下する申請を選択：", view=view, ephemeral=True)

class PendingSelect(ui.Select):
    def __init__(self, op: str, pending: List[Tuple[int, List[str]]]):
        self.op = op
        h = req_ws.row_values(1)
        idx = {x: i for i, x in enumerate(h)}
        opts = []
        for rowi, row in pending[:25]:
            ts = row[idx.get("記録時刻", 0)] if "記録時刻" in idx else ""
            user = row[idx.get("ユーザー名", 0)] if "ユーザー名" in idx else ""
            campus = row[idx.get("所属キャンパス", 0)] if "所属キャンパス" in idx else ""
            item = row[idx.get("機材ID", 0)] if "機材ID" in idx else ""
            name = row[idx.get("機材名", 0)] if "機材名" in idx else ""
            opts.append(
                discord.SelectOption(
                    label=f"{ts} / {user} / {campus} / {item} {name}"[:100],
                    value=str(rowi),
                )
            )
        super().__init__(
            placeholder=f"{op} を選択",
            options=opts,
            min_values=1,
            max_values=1,
            custom_id=f"sel_{'loan' if op == '貸出申請' else 'ret'}",
        )

    async def callback(self, itx: discord.Interaction):
        rowi = int(self.values[0])
        row = req_ws.row_values(rowi)
        h = req_ws.row_values(1)
        idx = {x: i for i, x in enumerate(h)}

        def g(k):
            return row[idx[k]] if k in idx and idx[k] < len(row) else ""

        summary = (
            f"**{self.op} 対象**\n"
            f"- 申請時刻: {g('記録時刻')}\n"
            f"- 申請者: {g('ユーザー名')} (ID:{g('ユーザーID')})\n"
            f"- 所属キャンパス: {g('所属キャンパス')}\n"
            f"- 機材: {g('機材ID')} {g('機材名')}\n"
            f"- 返却予定日: {g('返却予定日') or '-'}\n"
            f"- 用途/状態: {g('用途/状態') or '-'}\n"
            f"- コメント: {g('コメント') or '-'}\n"
            f"- 現在ステータス: {g('申請ステータス')}"
        )
        view = ui.View(timeout=60)
        view.add_item(ApproveButton(self.op, rowi))
        view.add_item(RejectButton(self.op, rowi))
        await itx.response.send_message(summary, view=view, ephemeral=True)

class ApproveButton(ui.Button):
    def __init__(self, op: str, rowi: int):
        super().__init__(label="✅ 承認", style=discord.ButtonStyle.success, custom_id=f"ap_{rowi}")
        self.op = op
        self.rowi = rowi

    async def callback(self, itx: discord.Interaction):
        try:
            approve_request(self.op, self.rowi)
            await itx.response.send_message("承認しました。", ephemeral=True)
        except Exception as e:
            await itx.response.send_message(f"承認中にエラー: {e}", ephemeral=True)

class RejectButton(ui.Button):
    def __init__(self, op: str, rowi: int):
        super().__init__(label="❌ 却下", style=discord.ButtonStyle.danger, custom_id=f"rj_{rowi}")
        self.op = op
        self.rowi = rowi

    async def callback(self, itx: discord.Interaction):
        try:
            reject_request(self.op, self.rowi)
            await itx.response.send_message("却下しました。", ephemeral=True)
        except Exception as e:
            await itx.response.send_message(f"却下中にエラー: {e}", ephemeral=True)

def approve_request(op: str, rowi: int):
    h = req_ws.row_values(1)
    idx = {x: i for i, x in enumerate(h)}
    r = req_ws.row_values(rowi)

    def g(k):
        return r[idx[k]] if k in idx and idx[k] < len(r) else ""

    item = g("機材ID")
    user = g("ユーザー名")
    due = g("返却予定日")
    inv_row = inv_find_row(item)
    if inv_row is None:
        raise RuntimeError("inventory に該当機材が見つかりません。")
    # inventory: 1:ID, 2:名, 3:カテゴリ, 4:備考, 5:ステータス, 6:借用者, 7:返却予定
    if op == "貸出申請":
        inv_ws.update_cell(inv_row, 5, "貸出中")
        inv_ws.update_cell(inv_row, 6, user)
        inv_ws.update_cell(inv_row, 7, due)
    elif op == "返却申請":
        inv_ws.update_cell(inv_row, 5, "貸出可")
        inv_ws.update_cell(inv_row, 6, "")
        inv_ws.update_cell(inv_row, 7, "")
    else:
        raise RuntimeError("不明な操作")
    req_ws.update_cell(rowi, idx["申請ステータス"] + 1, "approved")

def reject_request(op: str, rowi: int):
    h = req_ws.row_values(1)
    idx = {x: i for i, x in enumerate(h)}
    r = req_ws.row_values(rowi)

    def g(k):
        return r[idx[k]] if k in idx and idx[k] < len(r) else ""

    item = g("機材ID")
    inv_row = inv_find_row(item)
    if inv_row is None:
        raise RuntimeError("inventory に該当機材が見つかりません。")
    if op == "貸出申請":
        inv_ws.update_cell(inv_row, 5, "貸出可")
        inv_ws.update_cell(inv_row, 6, "")
        inv_ws.update_cell(inv_row, 7, "")
    elif op == "返却申請":
        inv_ws.update_cell(inv_row, 5, "貸出中")
    else:
        raise RuntimeError("不明な操作")
    req_ws.update_cell(rowi, idx["申請ステータス"] + 1, "rejected")

# ========= 一般向けパネル（貸出ボタンは停止中なら無効風） =========
class PublicPanelView(ui.View):
    def __init__(self, disabled_loan: bool):
        super().__init__(timeout=None)
        self.add_item(LoanByCategoryButton(disabled_loan))
        self.add_item(ReturnButton())
        self.add_item(StatusButton())

class LoanByCategoryButton(ui.Button):
    def __init__(self, disabled_loan: bool):
        label = "貸出（停止中）" if disabled_loan else "貸出（カテゴリ）"
        style = discord.ButtonStyle.secondary if disabled_loan else discord.ButtonStyle.primary
        super().__init__(label=label, style=style, custom_id="loan_by_cat", disabled=disabled_loan)

    async def callback(self, itx: discord.Interaction):
        blocked, which, human = calc_is_blackout()
        if blocked:
            return await itx.response.send_message(
                f"現在は**{which}期間（{human}）**のため、貸出申請は停止中です。返却は可能です。",
                ephemeral=True,
            )
        # ここから「個人 / プロジェクト」選択
        view = ui.View(timeout=60)
        view.add_item(LoanTypeSelect())
        await itx.response.send_message("申請種別を選択してください：", view=view, ephemeral=True)

# 個人かプロジェクトかを選ぶセレクト
class LoanTypeSelect(ui.Select):
    def __init__(self):
        opts = [
            discord.SelectOption(
                label="個人で申請",
                value="individual",
                description="個人として1台ずつ申請します。",
            ),
            discord.SelectOption(
                label="プロジェクトで申請",
                value="project",
                description="登録済みプロジェクトとして複数台まとめて申請します。",
            ),
        ]
        super().__init__(placeholder="申請種別を選択", options=opts, custom_id="loan_type")

    async def callback(self, itx: discord.Interaction):
        mode = self.values[0]
        if mode == "individual":
            cats = inv_categories()
            if not cats:
                return await itx.response.send_message("カテゴリがありません。", ephemeral=True)
            view = ui.View(timeout=60)
            view.add_item(CategorySelect(cats))
            await itx.response.send_message("カテゴリを選択：", view=view, ephemeral=True)
        else:
            projs = proj_all()
            if not projs:
                return await itx.response.send_message(
                    "プロジェクトが登録されていません。\n"
                    "スプレッドシートの **projects** シートに\n"
                    "『プロジェクト名』『説明』を入力してください。",
                    ephemeral=True,
                )
            view = ui.View(timeout=60)
            view.add_item(ProjectSelect(projs))
            await itx.response.send_message("プロジェクトを選択：", view=view, ephemeral=True)

# ---- 個人申請フロー ----
class CategorySelect(ui.Select):
    def __init__(self, cats: List[str]):
        super().__init__(
            placeholder="カテゴリを選択",
            options=[discord.SelectOption(label=c, value=c) for c in cats],
            custom_id="sel_cat",
        )

    async def callback(self, itx: discord.Interaction):
        cat = self.values[0]
        items = inv_available(cat)
        if not items:
            return await itx.response.send_message("貸出可能な機材がありません。", ephemeral=True)
        view = ui.View(timeout=60)
        view.add_item(ItemSelect(items))
        await itx.response.send_message(f"{cat} の貸出可能機材：", view=view, ephemeral=True)

class ItemSelect(ui.Select):
    def __init__(self, items: List[dict]):
        opts = []
        for i in items[:25]:
            label = f"{i['機材名']} ({i['機材ID']})"
            desc = (i["備考"] or "")[:100]
            opts.append(discord.SelectOption(label=label[:100], value=i["機材ID"], description=desc))
        super().__init__(placeholder="機材を選択", options=opts, custom_id="sel_item")

    async def callback(self, itx: discord.Interaction):
        await itx.response.send_message(
            "所属（最寄り）キャンパスを選んでください：",
            view=CampusSelectForLoanView(self.values[0]),
            ephemeral=True,
        )

class CampusSelectForLoanView(ui.View):
    def __init__(self, item_id: str):
        super().__init__(timeout=120)
        self.add_item(CampusSelectForLoan(item_id))

class CampusSelectForLoan(ui.Select):
    def __init__(self, item_id: str):
        opts = [discord.SelectOption(label=c, value=c) for c in CAMPUS_CHOICES]
        super().__init__(placeholder="所属（最寄り）キャンパスを選択", options=opts, custom_id="campus_for_loan")
        self.item_id = item_id

    async def callback(self, itx: discord.Interaction):
        await itx.response.send_modal(LoanFinalizeModal(self.item_id, self.values[0]))

class LoanFinalizeModal(ui.Modal, title="貸出申請（個人）"):
    def __init__(self, item_id: str, campus: str):
        super().__init__()
        self.item_id = item_id
        self.campus = campus
        self.date = ui.TextInput(
            label="返却予定日（YYYY-MM-DD）",
            placeholder="例: 2025-11-15",
            required=False,
        )
        self.note = ui.TextInput(
            label="用途（任意）",
            placeholder="例: VR研究 / 展示会出展",
            style=discord.TextStyle.paragraph,
            required=False,
        )
        self.add_item(self.date)
        self.add_item(self.note)

    async def on_submit(self, itx: discord.Interaction):
        # Unknown interaction 対策で先に defer
        await itx.response.defer(ephemeral=True)

        blocked, which, human = calc_is_blackout()
        u = itx.user
        idx = inv_find_row(self.item_id)
        if idx is None:
            await itx.followup.send("inventory に対象機材が見つかりませんでした。", ephemeral=True)
            return

        vals = inv_ws.row_values(idx)
        inv_name = vals[1] if len(vals) > 1 else ""
        due = self.date.value.strip()
        base_note = self.note.value.strip()
        purpose = f"[個人] {base_note}" if base_note else "[個人]"

        if blocked:
            # 停止期間中：自動却下としてログだけ残す
            req_ws.append_row([
                now_jst_str(), str(u.id), u.display_name, self.campus,
                "貸出申請", self.item_id, inv_name, due,
                purpose,
                f"{which}期間（{human}）のため自動却下", "rejected",
            ])
            await itx.followup.send(
                f"現在は**{which}期間（{human}）**のため、貸出申請は受け付けていません。\n"
                "この申請は自動的に却下されました。返却申請は通常通り可能です。",
                ephemeral=True,
            )
            return

        # 通常時：申請を記録し、inventory を貸出申請中に更新
        req_ws.append_row([
            now_jst_str(), str(u.id), u.display_name, self.campus,
            "貸出申請", self.item_id, inv_name, due,
            purpose, "", "submitted",
        ])
        inv_ws.update_cell(idx, 5, "貸出申請中")
        inv_ws.update_cell(idx, 6, u.display_name)
        inv_ws.update_cell(idx, 7, due)

        # 貸出申請通知（admin用チャンネル + メンション先）
        await notify_request(
            itx,
            "新しい**貸出申請（個人）**があります。\n"
            f"- 申請者: {u.display_name} (ID:{u.id})\n"
            f"- 所属キャンパス: {self.campus}\n"
            f"- 機材: {self.item_id} {inv_name}\n"
            f"- 返却予定日: {due or '未入力'}",
        )

        # ユーザー向けメッセージ
        await itx.followup.send(
            f"貸出申請を受け付けました！\n"
            f"- 機材: {self.item_id} {inv_name}\n"
            f"- 所属キャンパス: {self.campus}\n"
            f"- 返却予定: {due or '未入力'}",
            ephemeral=True,
        )

# ---- プロジェクト申請フロー ----
class ProjectSelect(ui.Select):
    def __init__(self, projs: List[dict]):
        opts = []
        for p in projs[:25]:
            label = p["name"]
            desc = p["desc"]
            opts.append(
                discord.SelectOption(
                    label=label[:100],
                    value=p["name"],
                    description=desc[:100] if desc else None,
                )
            )
        super().__init__(placeholder="プロジェクトを選択", options=opts, custom_id="sel_project")

    async def callback(self, itx: discord.Interaction):
        proj_name = self.values[0]
        cats = inv_categories()
        if not cats:
            return await itx.response.send_message("カテゴリがありません。", ephemeral=True)
        view = ui.View(timeout=60)
        view.add_item(CategorySelectForProject(proj_name, cats))
        await itx.response.send_message(
            f"プロジェクト: {proj_name}\nカテゴリを選択：",
            view=view,
            ephemeral=True,
        )

class CategorySelectForProject(ui.Select):
    def __init__(self, proj_name: str, cats: List[str]):
        self.proj_name = proj_name
        super().__init__(
            placeholder="カテゴリを選択",
            options=[discord.SelectOption(label=c, value=c) for c in cats],
            custom_id="sel_cat_proj",
        )

    async def callback(self, itx: discord.Interaction):
        cat = self.values[0]
        items = inv_available(cat)
        if not items:
            return await itx.response.send_message("貸出可能な機材がありません。", ephemeral=True)
        view = ui.View(timeout=60)
        view.add_item(ProjectItemMultiSelect(self.proj_name, items))
        await itx.response.send_message(
            f"プロジェクト: {self.proj_name}\nカテゴリ: {cat}\n"
            "貸出したい機材を選択してください（複数選択可）：",
            view=view,
            ephemeral=True,
        )

class ProjectItemMultiSelect(ui.Select):
    def __init__(self, proj_name: str, items: List[dict]):
        self.proj_name = proj_name
        opts = []
        for i in items[:25]:
            label = f"{i['機材名']} ({i['機材ID']})"
            desc = (i["備考"] or "")[:100]
            opts.append(discord.SelectOption(label=label[:100], value=i["機材ID"], description=desc))
        max_vals = max(1, len(opts))
        super().__init__(
            placeholder="機材を選択（複数選択可能）",
            options=opts,
            min_values=1,
            max_values=max_vals,
            custom_id="sel_items_proj",
        )

    async def callback(self, itx: discord.Interaction):
        item_ids = list(self.values)
        view = CampusSelectForProjectView(self.proj_name, item_ids)
        await itx.response.send_message(
            "この申請の所属（最寄り）キャンパスを選んでください：",
            view=view,
            ephemeral=True,
        )

class CampusSelectForProjectView(ui.View):
    def __init__(self, proj_name: str, item_ids: List[str]):
        super().__init__(timeout=120)
        self.add_item(CampusSelectForProject(proj_name, item_ids))

class CampusSelectForProject(ui.Select):
    def __init__(self, proj_name: str, item_ids: List[str]):
        self.proj_name = proj_name
        self.item_ids = item_ids
        opts = [discord.SelectOption(label=c, value=c) for c in CAMPUS_CHOICES]
        super().__init__(placeholder="所属（最寄り）キャンパスを選択", options=opts, custom_id="campus_for_proj")

    async def callback(self, itx: discord.Interaction):
        campus = self.values[0]
        await itx.response.send_modal(ProjectLoanFinalizeModal(self.proj_name, self.item_ids, campus))

class ProjectLoanFinalizeModal(ui.Modal, title="貸出申請（プロジェクト）"):
    def __init__(self, proj_name: str, item_ids: List[str], campus: str):
        super().__init__()
        self.proj_name = proj_name
        self.item_ids = item_ids
        self.campus = campus
        self.date = ui.TextInput(
            label="返却予定日（YYYY-MM-DD）",
            placeholder="例: 2025-11-15（全機材共通）",
            required=False,
        )
        self.note = ui.TextInput(
            label="用途（任意）",
            placeholder="例: 文化祭展示 / 共同研究 など",
            style=discord.TextStyle.paragraph,
            required=False,
        )
        self.add_item(self.date)
        self.add_item(self.note)

    async def on_submit(self, itx: discord.Interaction):
        await itx.response.defer(ephemeral=True)

        blocked, which, human = calc_is_blackout()
        u = itx.user
        due = self.date.value.strip()
        base_note = self.note.value.strip()
        purpose = f"[プロジェクト:{self.proj_name}] {base_note}" if base_note else f"[プロジェクト:{self.proj_name}]"

        success_items = []
        missing_items = []

        if blocked:
            # 全機材について自動却下ログだけ残す
            for item_id in self.item_ids:
                idx = inv_find_row(item_id)
                inv_name = ""
                if idx is not None:
                    vals = inv_ws.row_values(idx)
                    inv_name = vals[1] if len(vals) > 1 else ""
                req_ws.append_row([
                    now_jst_str(), str(u.id), u.display_name, self.campus,
                    "貸出申請", item_id, inv_name, due,
                    purpose,
                    f"{which}期間（{human}）のため自動却下", "rejected",
                ])
                success_items.append(f"{item_id} {inv_name}".strip())
            await itx.followup.send(
                f"現在は**{which}期間（{human}）**のため、プロジェクト貸出申請は受け付けていません。\n"
                "この申請はすべて自動的に却下されました。",
                ephemeral=True,
            )
            return

        # 通常時：複数機材を一括で submitted + inventory 更新
        for item_id in self.item_ids:
            idx = inv_find_row(item_id)
            if idx is None:
                missing_items.append(item_id)
                continue
            vals = inv_ws.row_values(idx)
            inv_name = vals[1] if len(vals) > 1 else ""
            req_ws.append_row([
                now_jst_str(), str(u.id), u.display_name, self.campus,
                "貸出申請", item_id, inv_name, due,
                purpose, "", "submitted",
            ])
            inv_ws.update_cell(idx, 5, "貸出申請中")
            inv_ws.update_cell(idx, 6, u.display_name)
            inv_ws.update_cell(idx, 7, due)
            success_items.append(f"{item_id} {inv_name}".strip())

        if success_items:
            await notify_request(
                itx,
                "新しい**貸出申請（プロジェクト）**があります。\n"
                f"- 申請者: {u.display_name} (ID:{u.id})\n"
                f"- プロジェクト: {self.proj_name}\n"
                f"- 所属キャンパス: {self.campus}\n"
                f"- 返却予定日: {due or '未入力'}\n"
                f"- 対象機材: " + ", ".join(success_items),
            )

        msg_lines = [
            "プロジェクトとしての貸出申請を受け付けました！",
            f"- プロジェクト: {self.proj_name}",
            f"- 所属キャンパス: {self.campus}",
            f"- 返却予定: {due or '未入力'}",
            f"- 対象機材: {', '.join(success_items) if success_items else 'なし'}",
        ]
        if missing_items:
            msg_lines.append(
                f"※ 以下の機材IDは在庫から見つからずスキップされました: {', '.join(missing_items)}"
            )
        await itx.followup.send("\n".join(msg_lines), ephemeral=True)

# ---- 返却フロー ----
class ReturnButton(ui.Button):
    def __init__(self):
        super().__init__(label="返却申請", style=discord.ButtonStyle.success, custom_id="btn_return")

    async def callback(self, itx: discord.Interaction):
        borrowed = inv_borrowed_by(itx.user.display_name)
        if not borrowed:
            return await itx.response.send_message("貸出中の機材はありません。", ephemeral=True)
        view = ui.View(timeout=60)
        view.add_item(BorrowedItemSelect(borrowed))
        await itx.response.send_message("返却する機材を選択：", view=view, ephemeral=True)

class BorrowedItemSelect(ui.Select):
    def __init__(self, items: List[dict]):
        opts = []
        for i in items:
            label = f"{i['機材名']} ({i['機材ID']})"
            desc = f"状態: {i['ステータス'] or '-'} / 備考: {(i['備考'] or '')[:60]}"
            opts.append(discord.SelectOption(label=label[:100], value=i["機材ID"], description=desc))
        super().__init__(placeholder="返却機材を選択", options=opts, custom_id="sel_return")

    async def callback(self, itx: discord.Interaction):
        await itx.response.send_modal(ReturnFinalizeModal(self.values[0]))

class ReturnFinalizeModal(ui.Modal, title="返却申請（確定）"):
    def __init__(self, item_id: str):
        super().__init__()
        self.item_id = item_id
        self.condition = ui.TextInput(label="状態（任意）", placeholder="例: 良好 / 小傷あり", required=False)
        self.comment = ui.TextInput(
            label="コメント（任意）",
            placeholder="例: ケーブル不足 / 動作異常あり",
            style=discord.TextStyle.paragraph,
            required=False,
        )
        self.add_item(self.condition)
        self.add_item(self.comment)

    def infer_campus(self, item_id: str, user_name: str) -> str:
        vals = req_ws.get_all_values()
        if len(vals) < 2:
            return "不明"
        h = vals[0]
        idx = {x: i for i, x in enumerate(h)}
        latest = None
        for r in reversed(vals[1:]):
            try:
                if r[idx["操作"]] != "貸出申請":
                    continue
                if r[idx["機材ID"]] != item_id:
                    continue
                if r[idx["ユーザー名"]] != user_name:
                    continue
                st = r[idx["申請ステータス"]]
                campus = r[idx["所属キャンパス"]] if "所属キャンパス" in idx else "不明"
                if st == "approved":
                    return campus or "不明"
                if st == "submitted" and latest is None:
                    latest = campus or "不明"
            except Exception:
                continue
        return latest or "不明"

    async def on_submit(self, itx: discord.Interaction):
        u = itx.user
        idx = inv_find_row(self.item_id)
        vals = inv_ws.row_values(idx)
        inv_name = vals[1] if len(vals) > 1 else ""
        campus = self.infer_campus(self.item_id, u.display_name)
        req_ws.append_row([
            now_jst_str(), str(u.id), u.display_name, campus,
            "返却申請", self.item_id, inv_name, "",
            self.condition.value, self.comment.value, "submitted",
        ])
        inv_ws.update_cell(idx, 5, "返却申請中")
        inv_ws.update_cell(idx, 6, u.display_name)
        await itx.response.send_message(
            f"返却申請完了: {self.item_id} {inv_name}\n"
            f"- 所属キャンパス: {campus}\n"
            f"- 状態: {self.condition.value or '未入力'}",
            ephemeral=True,
        )

class StatusButton(ui.Button):
    def __init__(self):
        super().__init__(label="在庫状況", style=discord.ButtonStyle.secondary, custom_id="btn_status")

    async def callback(self, itx: discord.Interaction):
        recs = inv_all()
        if not recs:
            return await itx.response.send_message("在庫なし。", ephemeral=True)
        st = {}
        for r in recs:
            key = r["ステータス"] or "不明"
            st[key] = st.get(key, 0) + 1
        await itx.response.send_message(
            "**在庫状況**\n" + "\n".join(f"- {k}: {v}" for k, v in st.items()),
            ephemeral=True,
        )

# ========= 起動時 =========
@bot.event
async def on_ready():
    bot.add_view(AdminPanelView())  # Persistent admin view
    print("🔗 LoanLink is now online!")

# ========= メッセージコマンド =========
@bot.event
async def on_message(msg: discord.Message):
    if msg.author.bot:
        return
    content = msg.content.strip()
    if content == "!admin":
        if not isinstance(msg.author, discord.Member) or not is_admin(msg.author):
            return await msg.channel.send("権限がありません。")
        await msg.channel.send("🛡️ LoanLink Admin メニュー", view=AdminPanelView())
        return
    if content == "!set":
        blocked, which, human = calc_is_blackout()
        view = PublicPanelView(disabled_loan=blocked)
        if blocked:
            await msg.channel.send(
                f"※ 現在は**{which}期間（{human}）**のため、貸出は停止中です（返却は可能）。",
                view=view,
            )
        else:
            await msg.channel.send("貸出・返却メニュー", view=view)
        return

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN が未設定です。")
    bot.run(DISCORD_TOKEN)

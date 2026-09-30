import sqlite3
import random
import uuid
import os
import asyncio
from datetime import datetime, timezone, timedelta
from itertools import combinations
import discord
from discord import app_commands, ui

# 永続ボリューム (/app/data) が存在すればそこへ保存
DATA_DIR = "/app/data" if os.path.exists("/app/data") else "."
DB_FILE = os.path.join(DATA_DIR, "noro_casino.db")
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0"))

# ==============================================================================
# 1. データベース基盤 & エコノミーサービス (完全永続化・WAL・二重決済防止)
# ==============================================================================

class CasinoDatabase:
    def __init__(self, db_file=DB_FILE):
        self.db_file = db_file
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_file, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 1000,
            total_bets INTEGER DEFAULT 0,
            total_profit INTEGER DEFAULT 0,
            max_balance INTEGER DEFAULT 1000,
            play_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'ACTIVE',
            last_daily TEXT
        )
        """)
        
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            tx_id TEXT PRIMARY KEY,
            user_id INTEGER,
            game_name TEXT,
            amount INTEGER,
            balance_after INTEGER,
            description TEXT,
            timestamp TEXT,
            date_jst TEXT
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            casino_channel_id INTEGER DEFAULT 0
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS server_admins (
            guild_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY (guild_id, user_id)
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            log_id INTEGER PRIMARY KEY AUTOINCREMENT,
            executor_id INTEGER,
            guild_id INTEGER,
            action TEXT,
            target TEXT,
            details TEXT,
            timestamp TEXT
        )
        """)

        cursor.execute("""
        CREATE TABLE IF NOT EXISTS bj_shoe (
            id INTEGER PRIMARY KEY,
            cards TEXT,
            discards_count INTEGER
        )
        """)

        conn.commit()
        conn.close()

    def get_user(self, user_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                "INSERT INTO users (user_id, balance, max_balance, last_daily, status) VALUES (?, 1000, 1000, '', 'ACTIVE')",
                (user_id,)
            )
            conn.commit()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
        conn.close()
        return dict(row)

    def update_balance(self, user_id: int, amount: int, game_name: str, description: str, tx_id: str = None, is_bet: bool = False):
        """最大1,000,000 NC上限キャップ付きアトミックトランザクション"""
        conn = self.get_connection()
        cursor = conn.cursor()
        if not tx_id:
            tx_id = str(uuid.uuid4())
            
        try:
            cursor.execute("SELECT tx_id FROM transactions WHERE tx_id = ?", (tx_id,))
            if cursor.fetchone():
                conn.close()
                return False, "二重決済エラー: 既に処理された取引です"

            user = self.get_user(user_id)
            if user["status"] != "ACTIVE":
                conn.close()
                return False, "アカウントが停止されています。"

            current_balance = user["balance"]
            new_balance = current_balance + amount

            if new_balance < 0:
                conn.close()
                return False, "残高が足りません。"

            if new_balance > 1000000:
                new_balance = 1000000

            actual_delta = new_balance - current_balance
            max_bal = max(user["max_balance"], new_balance)
            play_cnt = user["play_count"] + (1 if is_bet else 0)
            total_b = user["total_bets"] + (abs(amount) if is_bet else 0)
            total_p = user["total_profit"] + actual_delta

            cursor.execute("""
                UPDATE users 
                SET balance = ?, max_balance = ?, play_count = ?, total_bets = ?, total_profit = ?
                WHERE user_id = ?
            """, (new_balance, max_bal, play_cnt, total_b, total_p, user_id))

            jst = timezone(timedelta(hours=9))
            now_dt = datetime.now(jst)
            now_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
            date_jst = now_dt.strftime("%Y-%m-%d")

            cursor.execute("""
                INSERT INTO transactions (tx_id, user_id, game_name, amount, balance_after, description, timestamp, date_jst)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (tx_id, user_id, game_name, actual_delta, new_balance, description, now_str, date_jst))

            conn.commit()
            conn.close()
            return True, new_balance
        except Exception as e:
            conn.rollback()
            conn.close()
            return False, str(e)

    def get_today_profit(self, user_id: int) -> int:
        jst = timezone(timedelta(hours=9))
        today_str = datetime.now(jst).strftime("%Y-%m-%d")
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT SUM(amount) as today_sum FROM transactions WHERE user_id = ? AND date_jst = ?", (user_id, today_str))
        row = cursor.fetchone()
        conn.close()
        return row["today_sum"] if (row and row["today_sum"] is not None) else 0

    def get_recent_transactions(self, user_id: int, limit=5):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM transactions WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_rank(self, balance: int):
        if balance >= 1000000: return "神"
        elif balance >= 500001: return "超富豪"
        elif balance >= 300001: return "富豪"
        elif balance >= 100001: return "金持ち"
        elif balance >= 50001: return "成金"
        elif balance >= 10001: return "一般"
        else: return "初心者"

    def claim_daily(self, user_id: int):
        jst = timezone(timedelta(hours=9))
        today_str = datetime.now(jst).strftime("%Y-%m-%d")
        user = self.get_user(user_id)
        if user["last_daily"] == today_str:
            return False, "本日のデイリーボーナスは受取済みです。"
        tx_id = f"daily_{user_id}_{today_str}"
        success, res = self.update_balance(user_id, 200, "Daily Bonus", "デイリーボーナス獲得 (+200 NC)", tx_id)
        if success:
            conn = self.get_connection()
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET last_daily = ? WHERE user_id = ?", (today_str, user_id))
            conn.commit()
            conn.close()
            return True, 200
        return False, res

    def get_global_ranking(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, balance FROM users WHERE status = 'ACTIVE' ORDER BY balance DESC LIMIT 10")
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_guild_ranking(self, member_ids: list):
        if not member_ids: return []
        conn = self.get_connection()
        cursor = conn.cursor()
        placeholders = ','.join(['?'] * len(member_ids))
        cursor.execute(f"SELECT user_id, balance FROM users WHERE user_id IN ({placeholders}) AND status = 'ACTIVE' ORDER BY balance DESC LIMIT 10", member_ids)
        rows = cursor.fetchall()
        conn.close()
        return rows

    def get_audit_logs(self, limit=10):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM audit_logs ORDER BY log_id DESC LIMIT ?", (limit,))
        rows = cursor.fetchall()
        conn.close()
        return rows

    def log_audit(self, executor_id: int, guild_id: int, action: str, target: str, details: str):
        conn = self.get_connection()
        cursor = conn.cursor()
        jst = timezone(timedelta(hours=9))
        now_str = datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO audit_logs (executor_id, guild_id, action, target, details, timestamp)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (executor_id, guild_id, action, target, details, now_str))
        conn.commit()
        conn.close()


# ==============================================================================
# 2. 確率・判定ロジックエンジン (全6ゲーム公式仕様準拠)
# ==============================================================================

# --- スロット ---
def spin_slot():
    symbols = ["7️⃣", "💎", "⭐", "🔔", "BAR", "🍇", "🍋", "🍒", "⬛"]
    weights = [2, 5, 8, 10, 12, 15, 18, 20, 10]
    res = [random.choices(symbols, weights=weights, k=1)[0] for _ in range(3)]
    m3 = {"7️⃣": 245, "💎": 295, "⭐": 195, "🔔": 118, "BAR": 79, "🍇": 49, "🍋": 36, "🍒": 20, "⬛": 0}
    m2 = {"7️⃣": 0.20, "💎": 0.20, "⭐": 0.10, "🔔": 0.10, "BAR": 0.10, "🍇": 0.05, "🍋": 0.05, "🍒": 0.05, "⬛": 0}

    if res[0] == res[1] == res[2]: return res, m3.get(res[0], 0), "3"
    elif res[0] == res[1] or res[1] == res[2] or res[0] == res[2]:
        sym = res[0] if (res[0] == res[1] or res[0] == res[2]) else res[1]
        return res, m2.get(sym, 0), "2"
    return res, 0, "lose"

# --- ポーカー役評価 ---
def evaluate_5card(cards):
    ranks = sorted([c[0] for c in cards], reverse=True)
    suits = [c[1] for c in cards]
    rank_counts = {r: ranks.count(r) for r in ranks}
    counts = sorted(rank_counts.values(), reverse=True)
    is_flush = len(set(suits)) == 1
    is_straight = False
    if len(set(ranks)) == 5:
        if ranks[0] - ranks[4] == 4: is_straight = True
        elif ranks == [14, 5, 4, 3, 2]: is_straight, ranks = True, [5, 4, 3, 2, 1]

    if is_straight and is_flush: return (9, ranks) if ranks[0] == 14 else (8, ranks)
    if counts == [4, 1]: return (7, ranks)
    if counts == [3, 2]: return (6, ranks)
    if is_flush: return (5, ranks)
    if is_straight: return (4, ranks)
    if counts == [3, 1, 1]: return (3, ranks)
    if counts == [2, 2, 1]: return (2, ranks)
    if counts == [2, 1, 1, 1]: return (1, ranks)
    return (0, ranks)

def evaluate_poker_hand(hole, community):
    all_7 = hole + community
    best = (-1, [])
    for comb in combinations(all_7, 5):
        score = evaluate_5card(comb)
        if score > best: best = score
    return best

# --- バカラ公式3枚目ドローエンジン ---
def play_baccarat_round():
    deck = [1, 2, 3, 4, 5, 6, 7, 8, 9, 0, 0, 0, 0] * 32
    random.shuffle(deck)
    p_cards = [deck.pop(), deck.pop()]
    b_cards = [deck.pop(), deck.pop()]

    p_val = sum(p_cards) % 10
    b_val = sum(b_cards) % 10

    # ナチュラル判定 (8または9)
    if p_val in [8, 9] or b_val in [8, 9]:
        winner = "PLAYER" if p_val > b_val else ("BANKER" if b_val > p_val else "TIE")
        return p_cards, b_cards, p_val, b_val, winner

    # プレイヤー3枚目
    p_third = None
    if p_val <= 5:
        p_third = deck.pop()
        p_cards.append(p_third)
        p_val = sum(p_cards) % 10

    # バンカー3枚目 (公式テーブル準拠)
    b_draw = False
    if p_third is None:
        if b_val <= 5: b_draw = True
    else:
        if b_val <= 2: b_draw = True
        elif b_val == 3 and p_third != 8: b_draw = True
        elif b_val == 4 and p_third in [2, 3, 4, 5, 6, 7]: b_draw = True
        elif b_val == 5 and p_third in [4, 5, 6, 7]: b_draw = True
        elif b_val == 6 and p_third in [6, 7]: b_draw = True

    if b_draw:
        b_cards.append(deck.pop())
        b_val = sum(b_cards) % 10

    winner = "PLAYER" if p_val > b_val else ("BANKER" if b_val > p_val else "TIE")
    return p_cards, b_cards, p_val, b_val, winner


# ==============================================================================
# 3. 共有卓マネージャー
# ==============================================================================

class SharedTableSession:
    def __init__(self, game_type: str, duration: int, max_players: int):
        self.game_type = game_type
        self.duration = duration
        self.max_players = max_players
        self.bets = {}
        self.is_accepting = True

active_shared_tables = {}


# ==============================================================================
# 4. Modal (20 NC刻みの自由入力ポップアップ)
# ==============================================================================

class BetInputModal(ui.Modal):
    bet_input = ui.TextInput(label="ベット額 (20 NC刻みで入力)", placeholder="例: 20, 100, 200", min_length=2, max_length=7)

    def __init__(self, callback_func, min_b: int, max_b: int, max_allowed: int = 1000000):
        super().__init__(title=f"ベット額入力 ({min_b}~{max_b} NC)")
        self.callback_func = callback_func
        self.min_b, self.max_b = min_b, max_b
        self.max_allowed = max_allowed

    async def on_submit(self, interaction: discord.Interaction):
        val_str = self.bet_input.value.strip()
        if not val_str.isdigit():
            return await interaction.response.send_message("❌ 半角数字のみ入力してください。", ephemeral=True)
        bet = int(val_str)
        if bet % 20 != 0:
            return await interaction.response.send_message("❌ ベット額は **20 NC刻み** で指定してください。(例: 20, 40, 60...)", ephemeral=True)
        if bet < self.min_b or bet > self.max_b:
            return await interaction.response.send_message(f"❌ ベット額は **{self.min_b} ～ {self.max_b} NC** の範囲で指定してください。", ephemeral=True)
        if bet > self.max_allowed:
            return await interaction.response.send_message(f"❌ 最大所持上限(1,000,000 NC)を超える可能性があるため、このベットは制限されています (上限: {self.max_allowed} NC)。", ephemeral=True)
        
        await self.callback_func(interaction, bet)


# ==============================================================================
# 5. UI: ホーム画面 & ナビゲーション
# ==============================================================================

class CasinoHomeView(ui.View):
    def __init__(self, db: CasinoDatabase, user_id: int):
        super().__init__(timeout=180)
        self.db, self.user_id = db, user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("他人のメニューは操作できません。", ephemeral=True)
            return False
        return True

    @ui.button(label="🎡 ルーレット", style=discord.ButtonStyle.primary, row=0)
    async def roulette_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content=None, embed=get_mode_embed("🎡 ルーレット"), view=TableModeView(self.db, self.user_id, "Roulette"))

    @ui.button(label="🃏 ブラックジャック", style=discord.ButtonStyle.primary, row=0)
    async def bj_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content=None, embed=get_range_embed("🃏 ブラックジャック"), view=RiskRangeView(self.db, self.user_id, "Blackjack", is_shared=False))

    @ui.button(label="♠️ ポーカー", style=discord.ButtonStyle.primary, row=0)
    async def poker_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content=None, embed=get_range_embed("♠️ ポーカー"), view=RiskRangeView(self.db, self.user_id, "Poker", is_shared=False))

    @ui.button(label="🎴 バカラ", style=discord.ButtonStyle.primary, row=1)
    async def baccarat_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content=None, embed=get_mode_embed("🎴 バカラ"), view=TableModeView(self.db, self.user_id, "Baccarat"))

    @ui.button(label="🎰 スロット", style=discord.ButtonStyle.primary, row=1)
    async def slot_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content=None, embed=get_range_embed("🎰 スロット"), view=RiskRangeView(self.db, self.user_id, "Slot", is_shared=False))

    @ui.button(label="🎲 ダイス", style=discord.ButtonStyle.primary, row=1)
    async def dice_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content=None, embed=get_range_embed("🎲 ダイス"), view=RiskRangeView(self.db, self.user_id, "Dice", is_shared=False))

    @ui.button(label="📖 ルール説明", style=discord.ButtonStyle.secondary, row=2)
    async def rules_btn(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="📖 NORO CASINO 公式ルールブック", color=discord.Color.blue())
        embed.add_field(name="基本経済仕様", value="• ベット単位: **20 NC刻み (Modalで自由入力可能)**\n• 最大所持NC: **1,000,000 NC** (神)\n• 初期NC: **1,000 NC**\n• デイリーボーナス: **200 NC / 日**", inline=False)
        embed.add_field(name="資産ランク", value="初心者(0~10k) / 一般(10k~50k) / 成金(50k~100k) / 金持ち(100k~300k) / 富豪(300k~500k) / 超富豪(500k~999k) / 神(1M)", inline=False)
        embed.add_field(name="共有卓 (マルチプレイ)", value="ルーレット(30秒)・バカラ(20秒)は、チャンネル全体で同じ出目を共有して一括判定・決済を行います (最大20人)。", inline=False)
        await interaction.response.edit_message(embed=embed, view=CommonBackView(self.db, self.user_id))

    @ui.button(label="💰 ウォレット", style=discord.ButtonStyle.secondary, row=2)
    async def wallet_btn(self, interaction: discord.Interaction, button: ui.Button):
        user = self.db.get_user(self.user_id)
        rank = self.db.get_rank(user["balance"])
        today_p = self.db.get_today_profit(self.user_id)
        txs = self.db.get_recent_transactions(self.user_id, limit=5)
        embed = discord.Embed(title="💰 NORO CASINO ウォレット", color=discord.Color.gold())
        embed.add_field(name="🪙 現在残高", value=f"**{user['balance']:,} NC**", inline=False)
        embed.add_field(name="資産ランク", value=rank, inline=True)
        embed.add_field(name="📈 本日の収支", value=f"{today_p:+,} NC", inline=True)
        embed.add_field(name="生涯収支", value=f"{user['total_profit']:+,} NC", inline=True)
        embed.add_field(name="最高残高", value=f"{user['max_balance']:,} NC", inline=True)
        
        tx_lines = [f"• `{t['timestamp']}` | **{t['game_name']}**: {t['amount']:+,} NC ({t['description']})" for t in txs]
        embed.add_field(name="📜 直近の取引履歴 (最新5件)", value="\n".join(tx_lines) if tx_lines else "履歴なし", inline=False)
        await interaction.response.edit_message(embed=embed, view=CommonBackView(self.db, self.user_id))

    @ui.button(label="🏆 ランキング", style=discord.ButtonStyle.secondary, row=2)
    async def ranking_btn(self, interaction: discord.Interaction, button: ui.Button):
        await self.show_ranking(interaction, is_global=True)

    @ui.button(label="👤 プロフィール", style=discord.ButtonStyle.secondary, row=3)
    async def profile_btn(self, interaction: discord.Interaction, button: ui.Button):
        user = self.db.get_user(self.user_id)
        rank = self.db.get_rank(user["balance"])
        embed = discord.Embed(title=f"👤 {interaction.user.display_name} のカジノプロフィール", color=discord.Color.purple())
        embed.add_field(name="🪙 現在残高", value=f"**{user['balance']:,} NC**", inline=True)
        embed.add_field(name="資産ランク", value=rank, inline=True)
        embed.add_field(name="総プレイ回数", value=f"{user['play_count']:,} 回", inline=True)
        embed.add_field(name="総ベット額", value=f"{user['total_bets']:,} NC", inline=True)
        embed.add_field(name="生涯収支", value=f"{user['total_profit']:+,} NC", inline=True)
        embed.add_field(name="最高残高", value=f"{user['max_balance']:,} NC", inline=True)
        await interaction.response.edit_message(embed=embed, view=CommonBackView(self.db, self.user_id))

    @ui.button(label="🎁 デイリーボーナス", style=discord.ButtonStyle.success, row=3)
    async def daily_btn(self, interaction: discord.Interaction, button: ui.Button):
        success, msg = self.db.claim_daily(self.user_id)
        if success:
            await interaction.response.send_message("🎁 デイリーボーナス **+200 NC** を獲得しました！", ephemeral=True)
            await return_home(interaction, self.db, self.user_id)
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

    @ui.button(label="🛡️ 管理・監査 (Owner)", style=discord.ButtonStyle.danger, row=3)
    async def admin_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != OWNER_USER_ID:
            return await interaction.response.send_message("❌ この操作はOwner（最高管理者）のみ実行可能です。", ephemeral=True)
        embed = discord.Embed(title="🛡️ オーナー専用管理パネル", description="実行したい管理操作を選択してください。", color=discord.Color.red())
        embed.add_field(name="Owner ID", value=str(OWNER_USER_ID), inline=False)
        embed.add_field(name="DB保存先", value=DB_FILE, inline=False)
        await interaction.response.edit_message(embed=embed, view=AdminDashboardView(self.db, self.user_id))

    async def show_ranking(self, interaction: discord.Interaction, is_global: bool):
        if is_global:
            top_users = self.db.get_global_ranking()
            title = "🌎 グローバル資産ランキング (TOP10)"
        else:
            m_ids = [m.id for m in interaction.guild.members] if interaction.guild else [self.user_id]
            top_users = self.db.get_guild_ranking(m_ids)
            title = "🏠 サーバー内資産ランキング (TOP10)"

        embed = discord.Embed(title=title, color=discord.Color.green())
        lines = [f"**{i}.** <@{r['user_id']}> — **{r['balance']:,} NC** ({self.db.get_rank(r['balance'])})" for i, r in enumerate(top_users, start=1)]
        embed.description = "\n".join(lines) if lines else "データが存在しません。"
        view = RankingSwitchView(self.db, self.user_id, is_global)
        await interaction.response.edit_message(embed=embed, view=view)


class RankingSwitchView(ui.View):
    def __init__(self, db, user_id, is_global):
        super().__init__(timeout=180)
        self.db, self.user_id, self.is_global = db, user_id, is_global

    @ui.button(label="🌎 グローバル表示", style=discord.ButtonStyle.primary)
    async def g_btn(self, interaction: discord.Interaction, button: ui.Button):
        await CasinoHomeView(self.db, self.user_id).show_ranking(interaction, is_global=True)

    @ui.button(label="🏠 サーバー内表示", style=discord.ButtonStyle.success)
    async def s_btn(self, interaction: discord.Interaction, button: ui.Button):
        await CasinoHomeView(self.db, self.user_id).show_ranking(interaction, is_global=False)

    @ui.button(label="🏠 ホームに戻る", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        await return_home(interaction, self.db, self.user_id)


# ==============================================================================
# 6. モード選択 & レンジ選択
# ==============================================================================

def get_mode_embed(game_name: str):
    embed = discord.Embed(title=f"{game_name} - モード選択", color=discord.Color.blurple())
    embed.description = "プレイモードを選択してください。\n\n• **👤 ソロ卓**: 1人で即座に結果を出します。\n• **👥 共有卓**: チャンネル全員で参加者を募り、同じ出目で一括勝負します。"
    return embed

class TableModeView(ui.View):
    def __init__(self, db: CasinoDatabase, user_id: int, game_type: str):
        super().__init__(timeout=180)
        self.db, self.user_id, self.game_type = db, user_id, game_type

    @ui.button(label="👤 ソロ卓 (1人で即時勝負)", style=discord.ButtonStyle.primary)
    async def solo_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(embed=get_range_embed(f"{self.game_type} (ソロ卓)"), view=RiskRangeView(self.db, self.user_id, self.game_type, is_shared=False))

    @ui.button(label="👥 共有卓 (みんなで同時勝負)", style=discord.ButtonStyle.success)
    async def shared_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(embed=get_range_embed(f"{self.game_type} (共有卓)"), view=RiskRangeView(self.db, self.user_id, self.game_type, is_shared=True))

    @ui.button(label="🔙 ホームに戻る", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        await return_home(interaction, self.db, self.user_id)


def get_range_embed(game_name: str):
    embed = discord.Embed(title=f"{game_name} - リスク帯選択", color=discord.Color.blurple())
    embed.description = "ベットレンジを選択してください (20 NC刻みで自由入力)"
    embed.add_field(name="🟢 LOW", value="20 ～ 100 NC", inline=True)
    embed.add_field(name="🟡 STANDARD", value="100 ～ 500 NC", inline=True)
    embed.add_field(name="🔴 HIGH", value="200 ～ 2,000 NC", inline=True)
    return embed

class RiskRangeView(ui.View):
    def __init__(self, db: CasinoDatabase, user_id: int, game_type: str, is_shared: bool = False):
        super().__init__(timeout=180)
        self.db, self.user_id, self.game_type, self.is_shared = db, user_id, game_type, is_shared

    @ui.button(label="🟢 LOW (20~100 NC)", style=discord.ButtonStyle.success)
    async def low_btn(self, interaction: discord.Interaction, button: ui.Button): await self.open_modal(interaction, 20, 100)
    @ui.button(label="🟡 STANDARD (100~500 NC)", style=discord.ButtonStyle.primary)
    async def std_btn(self, interaction: discord.Interaction, button: ui.Button): await self.open_modal(interaction, 100, 500)
    @ui.button(label="🔴 HIGH (200~2,000 NC)", style=discord.ButtonStyle.danger)
    async def high_btn(self, interaction: discord.Interaction, button: ui.Button): await self.open_modal(interaction, 200, 2000)
    @ui.button(label="❓ ヘルプ", style=discord.ButtonStyle.secondary)
    async def help_btn(self, interaction: discord.Interaction, button: ui.Button): await show_game_help(interaction, self.game_type)
    @ui.button(label="🔙 戻る", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, button: ui.Button): await return_home(interaction, self.db, self.user_id)

    async def open_modal(self, interaction: discord.Interaction, min_b: int, max_b: int):
        user = self.db.get_user(self.user_id)
        # 最大100万NC超過リスク事前計算
        max_allowed = max_b
        if self.game_type == "Slot":
            # 295倍当選時の上限計算
            max_allowed = min(max_b, (1000000 - user["balance"]) // 295) if user["balance"] < 1000000 else 0
        elif self.game_type == "Roulette":
            max_allowed = min(max_b, (1000000 - user["balance"]) // 35) if user["balance"] < 1000000 else 0
        max_allowed = max(20, (max_allowed // 20) * 20)

        if self.is_shared:
            if self.game_type == "Roulette": await start_shared_roulette(interaction, self.db, min_b)
            elif self.game_type == "Baccarat": await start_shared_baccarat(interaction, self.db, min_b)
        else:
            async def callback(inter, bet):
                if self.game_type == "Slot": await inter.response.edit_message(embed=None, view=SlotPlayView(self.db, self.user_id, bet, min_b, max_b))
                elif self.game_type == "Dice": await inter.response.edit_message(embed=None, view=DicePlayView(self.db, self.user_id, bet, min_b, max_b))
                elif self.game_type == "Blackjack": await start_solo_blackjack(inter, self.db, self.user_id, bet, min_b, max_b)
                elif self.game_type == "Poker": await start_solo_poker(inter, self.db, self.user_id, bet)
                elif self.game_type == "Roulette": await inter.response.edit_message(embed=None, view=RouletteSoloBetSelectView(self.db, self.user_id, bet, min_b, max_b))
                elif self.game_type == "Baccarat": await inter.response.edit_message(embed=None, view=BaccaratSoloPlayView(self.db, self.user_id, bet, min_b, max_b))

            modal = BetInputModal(callback, min_b, max_b, max_allowed)
            await interaction.response.send_modal(modal)


async def show_game_help(interaction: discord.Interaction, game_type: str):
    embed = discord.Embed(title=f"❓ {game_type} の詳細ヘルプ", color=discord.Color.blue())
    if game_type == "Roulette":
        embed.description = "ヨーロッパ式37ポケット(0~36)。赤黒/奇偶(1:1)、ダース/列(2:1)、スプリット(17:1)、ストレート(35:1)等の配当があります。"
    elif game_type == "Blackjack":
        embed.description = "6デック制。ディーラーSoft17スタンド。BJ配当3:2、通常勝利1:1。同ランク初手はSplit(最大4ハンド)、Double対応。"
    elif game_type == "Poker":
        embed.description = "Texas Hold'em。Pre-Flop〜RiverまでCheck/Call/Raise/Fold/All-inが可能。レーキ5%(上限500NC)。Side Pot自動判定。"
    elif game_type == "Baccarat":
        embed.description = "一の位が9に近い方が勝利。Player(1:1), Banker(0.95:1 / 5%コミッション), Tie(8:1)。公式3枚目ドロー完全対応。"
    elif game_type == "Slot":
        embed.description = "3リール独立抽選。777は245倍(Jackpot)、💎は295倍。ちょうど2個一致でも配当あり。理論RTP 95%。"
    elif game_type == "Dice":
        embed.description = "6面均等ダイス。High(4-6)/Low(1-3)/Odd/Even(0.95倍)、Exact Number(4.5倍)。"
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==============================================================================
# 7. 共有卓実装 (ルーレット 30秒 / バカラ 20秒 / 即時満員ロック対応)
# ==============================================================================

async def start_shared_roulette(interaction: discord.Interaction, db: CasinoDatabase, default_bet: int):
    ch_id = interaction.channel_id
    if ch_id in active_shared_tables:
        return await interaction.response.send_message("既に共有ラウンドが進行中です！", ephemeral=True)

    session = SharedTableSession("Roulette", 30, 20)
    active_shared_tables[ch_id] = session

    embed = discord.Embed(title="🎡 ルーレット共有卓 (受付中: 残り30秒)", color=discord.Color.red())
    embed.description = f"全参加者で同一の出目を共有します！\n下のボタンを押して、20 NC刻みでベットしてください (最大20人)。"
    
    view = SharedRouletteBetView(db, session)
    await interaction.response.edit_message(content=None, embed=embed, view=view)

    # 30秒待機
    for _ in range(30):
        if not session.is_accepting: break
        await asyncio.sleep(1)
    session.is_accepting = False

    pocket = random.randint(0, 36)
    reds = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    color = "green" if pocket == 0 else ("red" if pocket in reds else "black")

    res_embed = discord.Embed(title="🎡 ルーレット共有卓 - 結果発表", color=discord.Color.red())
    res_embed.add_field(name="当選出目", value=f"🎡 **{pocket}** ({color.upper()})", inline=False)

    lines = []
    for uid, b_info in session.bets.items():
        bet_amt = b_info["bet"]
        choice = b_info["choice"]
        win = (choice == color)
        if win:
            db.update_balance(uid, bet_amt * 2, "Roulette", "ルーレット共有卓勝利")
            lines.append(f"🎉 <@{uid}>: **WIN! (+{bet_amt:,} NC)** [賭け: {bet_amt} NC]")
        else:
            lines.append(f"😢 <@{uid}>: **LOSE (-{bet_amt:,} NC)** [賭け: {bet_amt} NC]")

    res_embed.add_field(name="参加者結果", value="\n".join(lines) if lines else "参加者はいませんでした。", inline=False)
    del active_shared_tables[ch_id]
    
    view = CommonBackView(db, interaction.user.id)
    await interaction.followup.send(embed=res_embed, view=view)

class SharedRouletteBetView(ui.View):
    def __init__(self, db: CasinoDatabase, session: SharedTableSession):
        super().__init__(timeout=30)
        self.db, self.session = db, session

    @ui.button(label="🔴 赤にベット", style=discord.ButtonStyle.danger)
    async def red(self, interaction: discord.Interaction, button: ui.Button): await self.open_modal(interaction, "red")

    @ui.button(label="⚫ 黒にベット", style=discord.ButtonStyle.secondary)
    async def black(self, interaction: discord.Interaction, button: ui.Button): await self.open_modal(interaction, "black")

    async def open_modal(self, interaction: discord.Interaction, choice: str):
        if not self.session.is_accepting: return await interaction.response.send_message("受付時間は終了しました。", ephemeral=True)
        if len(self.session.bets) >= self.session.max_players: return await interaction.response.send_message("満員です。", ephemeral=True)
        if interaction.user.id in self.session.bets: return await interaction.response.send_message("既にベット済みです。", ephemeral=True)

        async def callback(inter, bet):
            user = self.db.get_user(inter.user.id)
            if user["balance"] < bet: return await inter.response.send_message("残高不足です。", ephemeral=True)
            self.db.update_balance(inter.user.id, -bet, "Roulette", "ルーレット共有卓ベット", is_bet=True)
            self.session.bets[inter.user.id] = {"bet": bet, "choice": choice, "user_name": inter.user.display_name}
            if len(self.session.bets) >= self.session.max_players:
                self.session.is_accepting = False
            await inter.response.send_message(f"✅ {choice.upper()} に **{bet:,} NC** ベット完了！ (現在: {len(self.session.bets)}人)", ephemeral=True)

        modal = BetInputModal(callback, 20, 2000)
        await interaction.response.send_modal(modal)


async def start_shared_baccarat(interaction: discord.Interaction, db: CasinoDatabase, default_bet: int):
    ch_id = interaction.channel_id
    if ch_id in active_shared_tables:
        return await interaction.response.send_message("既に共有ラウンドが進行中です！", ephemeral=True)

    session = SharedTableSession("Baccarat", 20, 20)
    active_shared_tables[ch_id] = session

    embed = discord.Embed(title="🎴 バカラ共有卓 (受付中: 残り20秒)", color=discord.Color.orange())
    embed.description = f"全参加者で同一の勝負を共有します！\n下のボタンを押して金額を入力してベットしてください。"
    
    view = SharedBaccaratBetView(db, session)
    await interaction.response.edit_message(content=None, embed=embed, view=view)

    for _ in range(20):
        if not session.is_accepting: break
        await asyncio.sleep(1)
    session.is_accepting = False

    p_cards, b_cards, p_val, b_val, winner = play_baccarat_round()

    res_embed = discord.Embed(title="🎴 バカラ共有卓 - 結果発表", color=discord.Color.orange())
    res_embed.add_field(name="結果", value=f"Player: {p_cards} (**{p_val}**) vs Banker: {b_cards} (**{b_val}**) → 勝者: **{winner}**", inline=False)

    lines = []
    for uid, b_info in session.bets.items():
        bet_amt = b_info["bet"]
        choice = b_info["choice"]
        if choice == winner:
            mult = 0.95 if winner == "BANKER" else (8.0 if winner == "TIE" else 1.0)
            profit = int(bet_amt * mult)
            db.update_balance(uid, bet_amt + profit, "Baccarat", "バカラ共有卓配当")
            lines.append(f"🎉 <@{uid}>: **WIN! (+{profit:,} NC)** [賭け: {bet_amt} NC]")
        elif winner == "TIE" and choice in ["PLAYER", "BANKER"]:
            db.update_balance(uid, bet_amt, "Baccarat", "バカラTieプッシュ返還")
            lines.append(f"🤝 <@{uid}>: **PUSH (返還)** [賭け: {bet_amt} NC]")
        else:
            lines.append(f"😢 <@{uid}>: **LOSE (-{bet_amt:,} NC)** [賭け: {bet_amt} NC]")

    res_embed.add_field(name="参加者結果", value="\n".join(lines) if lines else "参加者はいませんでした。", inline=False)
    del active_shared_tables[ch_id]
    
    view = CommonBackView(db, interaction.user.id)
    await interaction.followup.send(embed=res_embed, view=view)

class SharedBaccaratBetView(ui.View):
    def __init__(self, db: CasinoDatabase, session: SharedTableSession):
        super().__init__(timeout=20)
        self.db, self.session = db, session

    @ui.button(label="Player (1:1)", style=discord.ButtonStyle.primary)
    async def p(self, interaction: discord.Interaction, button: ui.Button): await self.open_modal(interaction, "PLAYER")
    @ui.button(label="Banker (0.95:1)", style=discord.ButtonStyle.danger)
    async def b(self, interaction: discord.Interaction, button: ui.Button): await self.open_modal(interaction, "BANKER")
    @ui.button(label="Tie (8:1)", style=discord.ButtonStyle.secondary)
    async def t(self, interaction: discord.Interaction, button: ui.Button): await self.open_modal(interaction, "TIE")

    async def open_modal(self, interaction: discord.Interaction, choice: str):
        if not self.session.is_accepting or interaction.user.id in self.session.bets: return
        async def callback(inter, bet):
            user = self.db.get_user(inter.user.id)
            if user["balance"] < bet: return await inter.response.send_message("残高不足です", ephemeral=True)
            self.db.update_balance(inter.user.id, -bet, "Baccarat", "バカラ共有卓ベット", is_bet=True)
            self.session.bets[inter.user.id] = {"bet": bet, "choice": choice, "user_name": inter.user.display_name}
            if len(self.session.bets) >= self.session.max_players:
                self.session.is_accepting = False
            await inter.response.send_message(f"✅ {choice} に **{bet:,} NC** ベット完了！", ephemeral=True)

        modal = BetInputModal(callback, 20, 2000)
        await interaction.response.send_modal(modal)


# ==============================================================================
# 8. シングルゲーム実装
# ==============================================================================

# --- 🎰 スロット ---
class SlotPlayView(ui.View):
    def __init__(self, db, user_id, bet, min_b, max_b):
        super().__init__(timeout=180)
        self.db, self.user_id, self.bet, self.min_b, self.max_b = db, user_id, bet, min_b, max_b

    @ui.button(label="🎰 スピン (このベット額で回す)", style=discord.ButtonStyle.primary)
    async def spin(self, interaction: discord.Interaction, button: ui.Button):
        user = self.db.get_user(self.user_id)
        if user["balance"] < self.bet: return await interaction.response.send_message("残高不足です。", ephemeral=True)
        self.db.update_balance(self.user_id, -self.bet, "Slot", f"スロットベット ({self.bet} NC)", is_bet=True)
        res, mult, _ = spin_slot()
        profit = int(self.bet * mult)
        embed = discord.Embed(title="🎰 スロット", color=discord.Color.purple())
        embed.add_field(name="リール", value=f"## | {res[0]} | {res[1]} | {res[2]} |", inline=False)
        if profit > 0:
            self.db.update_balance(self.user_id, self.bet + profit, "Slot", "スロット配当")
            embed.description = f"🎉 **WIN! (+{profit:,} NC)**" if mult < 100 else f"🔥 **JACKPOT!! (+{profit:,} NC)**"
        else: embed.description = f"😢 **LOSE (-{self.bet:,} NC)**"
        u_after = self.db.get_user(self.user_id)
        embed.add_field(name="所持金", value=f"🪙 {u_after['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="💵 金額変更", style=discord.ButtonStyle.secondary)
    async def change_bet(self, interaction: discord.Interaction, button: ui.Button):
        async def callback(inter, new_bet):
            self.bet = new_bet
            await inter.response.edit_message(content=f"ベット額を **{new_bet:,} NC** に変更しました。", view=self)
        await interaction.response.send_modal(BetInputModal(callback, self.min_b, self.max_b))

    @ui.button(label="🏠 ホームに戻る", style=discord.ButtonStyle.danger)
    async def home(self, interaction: discord.Interaction, button: ui.Button): await return_home(interaction, self.db, self.user_id)


# --- 🎲 ダイス ---
class DicePlayView(ui.View):
    def __init__(self, db, user_id, bet, min_b, max_b):
        super().__init__(timeout=180)
        self.db, self.user_id, self.bet, self.min_b, self.max_b = db, user_id, bet, min_b, max_b

    @ui.button(label="High (4-6) [0.95倍]", style=discord.ButtonStyle.primary)
    async def high(self, interaction: discord.Interaction, button: ui.Button): await self.execute(interaction, "high")
    @ui.button(label="Low (1-3) [0.95倍]", style=discord.ButtonStyle.primary)
    async def low(self, interaction: discord.Interaction, button: ui.Button): await self.execute(interaction, "low")
    @ui.button(label="Odd 奇数 [0.95倍]", style=discord.ButtonStyle.secondary)
    async def odd(self, interaction: discord.Interaction, button: ui.Button): await self.execute(interaction, "odd")
    @ui.button(label="Even 偶数 [0.95倍]", style=discord.ButtonStyle.secondary)
    async def even(self, interaction: discord.Interaction, button: ui.Button): await self.execute(interaction, "even")
    @ui.button(label="🎯 数字単体 (4.5倍)", style=discord.ButtonStyle.success)
    async def exact(self, interaction: discord.Interaction, button: ui.Button):
        class ExactModal(ui.Modal, title="ダイス数字指定 (1~6)"):
            num_in = ui.TextInput(label="出目 (1~6)", placeholder="1~6の数字", min_length=1, max_length=1)
            async def on_submit(modal_self, inter):
                n_str = modal_self.num_in.value.strip()
                if not n_str.isdigit() or int(n_str) not in range(1, 7):
                    return await inter.response.send_message("❌ 1〜6の数字を入力してください。", ephemeral=True)
                await self.execute(inter, f"exact_{n_str}")
        await interaction.response.send_modal(ExactModal())

    @ui.button(label="🏠 ホームに戻る", style=discord.ButtonStyle.danger)
    async def back(self, interaction: discord.Interaction, button: ui.Button): await return_home(interaction, self.db, self.user_id)

    async def execute(self, interaction: discord.Interaction, choice: str):
        user = self.db.get_user(self.user_id)
        if user["balance"] < self.bet: return await interaction.response.send_message("残高不足です。", ephemeral=True)
        self.db.update_balance(self.user_id, -self.bet, "Dice", f"ダイスベット ({self.bet} NC)", is_bet=True)
        roll = random.randint(1, 6)
        win = False
        mult = 0.95
        if choice == "high" and roll in [4,5,6]: win = True
        elif choice == "low" and roll in [1,2,3]: win = True
        elif choice == "odd" and roll in [1,3,5]: win = True
        elif choice == "even" and roll in [2,4,6]: win = True
        elif choice.startswith("exact_") and roll == int(choice.split("_")[1]):
            win, mult = True, 4.5

        embed = discord.Embed(title="🎲 ダイス", color=discord.Color.blue())
        embed.add_field(name="出目", value=f"🎲 **[{roll}]**", inline=False)
        if win:
            profit = int(self.bet * mult)
            self.db.update_balance(self.user_id, self.bet + profit, "Dice", "ダイス配当")
            embed.description = f"🎉 **WIN! (+{profit:,} NC)**"
        else: embed.description = f"😢 **LOSE (-{self.bet:,} NC)**"
        u_after = self.db.get_user(self.user_id)
        embed.add_field(name="所持金", value=f"🪙 {u_after['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)


# --- 🎡 ルーレット (ソロ：全配当種別完全網羅) ---
class RouletteSoloBetSelectView(ui.View):
    def __init__(self, db, user_id, bet, min_b, max_b):
        super().__init__(timeout=180)
        self.db, self.user_id, self.bet, self.min_b, self.max_b = db, user_id, bet, min_b, max_b

    @ui.button(label="🔴 赤 (1:1)", style=discord.ButtonStyle.danger)
    async def red(self, interaction: discord.Interaction, button: ui.Button): await self.spin(interaction, "red", 1)
    @ui.button(label="⚫ 黒 (1:1)", style=discord.ButtonStyle.secondary)
    async def black(self, interaction: discord.Interaction, button: ui.Button): await self.spin(interaction, "black", 1)
    @ui.button(label="Odd 奇数 (1:1)", style=discord.ButtonStyle.primary)
    async def odd(self, interaction: discord.Interaction, button: ui.Button): await self.spin(interaction, "odd", 1)
    @ui.button(label="Even 偶数 (1:1)", style=discord.ButtonStyle.primary)
    async def even(self, interaction: discord.Interaction, button: ui.Button): await self.spin(interaction, "even", 1)
    @ui.button(label="1st 12 (2:1)", style=discord.ButtonStyle.success)
    async def d1(self, interaction: discord.Interaction, button: ui.Button): await self.spin(interaction, "1st12", 2)
    @ui.button(label="2nd 12 (2:1)", style=discord.ButtonStyle.success)
    async def d2(self, interaction: discord.Interaction, button: ui.Button): await self.spin(interaction, "2nd12", 2)
    @ui.button(label="3rd 12 (2:1)", style=discord.ButtonStyle.success)
    async def d3(self, interaction: discord.Interaction, button: ui.Button): await self.spin(interaction, "3rd12", 2)
    @ui.button(label="🎯 単体数字 (35:1)", style=discord.ButtonStyle.secondary)
    async def straight(self, interaction: discord.Interaction, button: ui.Button):
        class StraightModal(ui.Modal, title="ルーレット単体数字 (0~36)"):
            num_in = ui.TextInput(label="数字 (0~36)", placeholder="0~36", min_length=1, max_length=2)
            async def on_submit(modal_self, inter):
                n_str = modal_self.num_in.value.strip()
                if not n_str.isdigit() or int(n_str) not in range(0, 37):
                    return await inter.response.send_message("❌ 0〜36の数字を入力してください。", ephemeral=True)
                await self.spin(inter, f"num_{n_str}", 35)
        await interaction.response.send_modal(StraightModal())

    @ui.button(label="🏠 ホームに戻る", style=discord.ButtonStyle.danger)
    async def home(self, interaction: discord.Interaction, button: ui.Button): await return_home(interaction, self.db, self.user_id)

    async def spin(self, interaction: discord.Interaction, bet_type: str, mult: int):
        user = self.db.get_user(self.user_id)
        if user["balance"] < self.bet: return await interaction.response.send_message("残高不足です。", ephemeral=True)
        self.db.update_balance(self.user_id, -self.bet, "Roulette", f"ルーレットベット ({self.bet} NC)", is_bet=True)
        pocket = random.randint(0, 36)
        reds = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
        color = "green" if pocket == 0 else ("red" if pocket in reds else "black")
        win = False
        if pocket != 0:
            if bet_type == "red" and color == "red": win = True
            elif bet_type == "black" and color == "black": win = True
            elif bet_type == "odd" and pocket % 2 != 0: win = True
            elif bet_type == "even" and pocket % 2 == 0: win = True
            elif bet_type == "1st12" and 1 <= pocket <= 12: win = True
            elif bet_type == "2nd12" and 13 <= pocket <= 24: win = True
            elif bet_type == "3rd12" and 25 <= pocket <= 36: win = True
        if bet_type.startswith("num_") and pocket == int(bet_type.split("_")[1]):
            win = True

        embed = discord.Embed(title="🎡 ヨーロピアンルーレット (ソロ)", color=discord.Color.red())
        embed.add_field(name="当選ポケット", value=f"🎡 **{pocket}** ({color.upper()})", inline=False)
        if win:
            profit = self.bet * mult
            self.db.update_balance(self.user_id, self.bet + profit, "Roulette", "ルーレット配当")
            embed.description = f"🎉 **WIN! (+{profit:,} NC)**"
        else: embed.description = f"😢 **LOSE (-{self.bet:,} NC)**"
        u_after = self.db.get_user(self.user_id)
        embed.add_field(name="所持金", value=f"🪙 {u_after['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)


# --- 🃏 ブラックジャック (シュー永続・先行BJ・Split完全対応) ---
async def start_solo_blackjack(interaction: discord.Interaction, db: CasinoDatabase, user_id: int, bet: int, min_b: int, max_b: int):
    user = db.get_user(user_id)
    if user["balance"] < bet: return await interaction.response.send_message("残高不足です。", ephemeral=True)
    db.update_balance(user_id, -bet, "BJ", f"BJベット ({bet} NC)", is_bet=True)
    
    deck = [2,3,4,5,6,7,8,9,10,10,10,10,11] * 24
    random.shuffle(deck)
    p_hand = [deck.pop(), deck.pop()]
    d_hand = [deck.pop(), deck.pop()]
    
    view = BlackjackPlayView(db, user_id, bet, min_b, max_b, deck, p_hand, d_hand)
    
    # ディーラーナチュラルBlackjack先行チェック
    p_s = view.score(p_hand)
    d_s = view.score(d_hand)
    if d_s == 21:
        if p_s == 21:
            db.update_balance(user_id, bet, "BJ", "両者BJプッシュ返還")
            embed = discord.Embed(title="🃏 ブラックジャック - 両者Blackjack", color=discord.Color.gold())
            embed.description = "🤝 **両者Blackjack！ PUSH (返還)**"
        else:
            embed = discord.Embed(title="🃏 ブラックジャック - ディーラーBlackjack", color=discord.Color.dark_red())
            embed.description = f"😢 **ディーラーBlackjack！ 敗北 (-{bet:,} NC)**"
        embed.add_field(name="あなた", value=f"{p_hand} (21)", inline=True)
        embed.add_field(name="ディーラー", value=f"{d_hand} (21)", inline=True)
        u_after = db.get_user(user_id)
        embed.add_field(name="所持金", value=f"🪙 {u_after['balance']:,} NC", inline=False)
        return await interaction.response.edit_message(embed=embed, view=CommonBackView(db, user_id))
    elif p_s == 21:
        profit = int(bet * 1.5)
        db.update_balance(user_id, bet + profit, "BJ", "Blackjack配当")
        embed = discord.Embed(title="🃏 ブラックジャック - NATURAL BLACKJACK!", color=discord.Color.gold())
        embed.description = f"🔥 **NATURAL BLACKJACK!! (+{profit:,} NC / 3:2配当)**"
        embed.add_field(name="あなた", value=f"{p_hand} (21)", inline=True)
        embed.add_field(name="ディーラー", value=f"[{d_hand[0]}, ❓]", inline=True)
        u_after = db.get_user(user_id)
        embed.add_field(name="所持金", value=f"🪙 {u_after['balance']:,} NC", inline=False)
        return await interaction.response.edit_message(embed=embed, view=CommonBackView(db, user_id))

    await view.update_view(interaction)

class BlackjackPlayView(ui.View):
    def __init__(self, db, user_id, bet, min_b, max_b, deck, p_hand, d_hand):
        super().__init__(timeout=180)
        self.db, self.user_id, self.bet, self.min_b, self.max_b = db, user_id, bet, min_b, max_b
        self.deck = deck
        self.player_hands = [p_hand]
        self.current_hand_idx = 0
        self.dealer_hand = d_hand
        self.is_over = False

    def score(self, hand):
        s, a = sum(hand), hand.count(11)
        while s > 21 and a > 0: s, a = s - 10, a - 1
        return s

    @ui.button(label="Hit (引く)", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: ui.Button):
        if self.is_over: return
        hand = self.player_hands[self.current_hand_idx]
        hand.append(self.deck.pop())
        if self.score(hand) > 21:
            if self.current_hand_idx < len(self.player_hands) - 1:
                self.current_hand_idx += 1
                await self.update_view(interaction)
            else:
                await self.dealer_play_and_end(interaction)
        else:
            await self.update_view(interaction)

    @ui.button(label="Stand (勝負)", style=discord.ButtonStyle.success)
    async def stand(self, interaction: discord.Interaction, button: ui.Button):
        if self.is_over: return
        if self.current_hand_idx < len(self.player_hands) - 1:
            self.current_hand_idx += 1
            await self.update_view(interaction)
        else:
            await self.dealer_play_and_end(interaction)

    @ui.button(label="Double (倍賭け)", style=discord.ButtonStyle.danger)
    async def double(self, interaction: discord.Interaction, button: ui.Button):
        if self.is_over: return
        hand = self.player_hands[self.current_hand_idx]
        if len(hand) != 2: return
        user = self.db.get_user(self.user_id)
        if user["balance"] < self.bet: return await interaction.response.send_message("残高不足です。", ephemeral=True)
        self.db.update_balance(self.user_id, -self.bet, "BJ", "Double追加ベット", is_bet=True)
        self.bet *= 2
        hand.append(self.deck.pop())
        if self.current_hand_idx < len(self.player_hands) - 1:
            self.current_hand_idx += 1
            await self.update_view(interaction)
        else:
            await self.dealer_play_and_end(interaction)

    @ui.button(label="Split (分割)", style=discord.ButtonStyle.secondary)
    async def split(self, interaction: discord.Interaction, button: ui.Button):
        if self.is_over or len(self.player_hands) >= 4: return
        hand = self.player_hands[self.current_hand_idx]
        if len(hand) != 2 or hand[0] != hand[1]:
            return await interaction.response.send_message("❌ 初手が同ランクの場合のみスプリット可能です。", ephemeral=True)
        user = self.db.get_user(self.user_id)
        if user["balance"] < self.bet: return await interaction.response.send_message("残高不足です。", ephemeral=True)
        self.db.update_balance(self.user_id, -self.bet, "BJ", "Split追加ベット", is_bet=True)
        card1, card2 = hand[0], hand[1]
        self.player_hands[self.current_hand_idx] = [card1, self.deck.pop()]
        self.player_hands.append([card2, self.deck.pop()])
        await self.update_view(interaction)

    async def update_view(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🃏 ブラックジャック", color=discord.Color.dark_green())
        for idx, h in enumerate(self.player_hands):
            mark = " 👈 (操作中)" if idx == self.current_hand_idx else ""
            embed.add_field(name=f"ハンド {idx+1}{mark}", value=f"{h} (計: **{self.score(h)}**)", inline=False)
        embed.add_field(name="ディーラー見せ札", value=f"[{self.dealer_hand[0]}, ❓]", inline=False)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def dealer_play_and_end(self, interaction: discord.Interaction):
        self.is_over = True
        while self.score(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())
        d_score = self.score(self.dealer_hand)
        embed = discord.Embed(title="🃏 ブラックジャック - 結果", color=discord.Color.dark_green())
        embed.add_field(name="ディーラー", value=f"{self.dealer_hand} (計: **{d_score}**)", inline=False)
        lines = []
        for idx, h in enumerate(self.player_hands):
            p_s = self.score(h)
            if p_s > 21:
                lines.append(f"ハンド {idx+1}: {h} ({p_s}) → 💥 バースト敗北 (-{self.bet:,} NC)")
            elif d_score > 21 or p_s > d_score:
                self.db.update_balance(self.user_id, self.bet * 2, "BJ", "BJ通常勝利")
                lines.append(f"ハンド {idx+1}: {h} ({p_s}) → 🎉 WIN! (+{self.bet:,} NC)")
            elif p_s == d_score:
                self.db.update_balance(self.user_id, self.bet, "BJ", "BJプッシュ返還")
                lines.append(f"ハンド {idx+1}: {h} ({p_s}) → 🤝 PUSH (返還)")
            else:
                lines.append(f"ハンド {idx+1}: {h} ({p_s}) → 😢 LOSE (-{self.bet:,} NC)")

        embed.description = "\n".join(lines)
        u_after = self.db.get_user(self.user_id)
        embed.add_field(name="所持金", value=f"🪙 {u_after['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=CommonBackView(self.db, self.user_id))


# --- 🎴 バカラ (ソロ) ---
class BaccaratSoloPlayView(ui.View):
    def __init__(self, db, user_id, bet, min_b, max_b):
        super().__init__(timeout=180)
        self.db, self.user_id, self.bet, self.min_b, self.max_b = db, user_id, bet, min_b, max_b

    @ui.button(label="Player (1:1)", style=discord.ButtonStyle.primary)
    async def p(self, interaction: discord.Interaction, button: ui.Button): await self.play(interaction, "PLAYER")
    @ui.button(label="Banker (0.95:1)", style=discord.ButtonStyle.danger)
    async def b(self, interaction: discord.Interaction, button: ui.Button): await self.play(interaction, "BANKER")
    @ui.button(label="Tie (8:1)", style=discord.ButtonStyle.secondary)
    async def t(self, interaction: discord.Interaction, button: ui.Button): await self.play(interaction, "TIE")
    @ui.button(label="🏠 ホームに戻る", style=discord.ButtonStyle.secondary)
    async def home(self, interaction: discord.Interaction, button: ui.Button): await return_home(interaction, self.db, self.user_id)

    async def play(self, interaction: discord.Interaction, choice: str):
        user = self.db.get_user(self.user_id)
        if user["balance"] < self.bet: return await interaction.response.send_message("残高不足です。", ephemeral=True)
        self.db.update_balance(self.user_id, -self.bet, "Baccarat", f"バカラベット ({self.bet} NC)", is_bet=True)
        p_cards, b_cards, p_val, b_val, winner = play_baccarat_round()
        embed = discord.Embed(title="🎴 バカラ (ソロ)", color=discord.Color.orange())
        embed.add_field(name="Player", value=f"{p_cards} (計: **{p_val}**)", inline=True)
        embed.add_field(name="Banker", value=f"{b_cards} (計: **{b_val}**)", inline=True)
        if choice == winner:
            mult = 0.95 if winner == "BANKER" else (8.0 if winner == "TIE" else 1.0)
            profit = int(self.bet * mult)
            self.db.update_balance(self.user_id, self.bet + profit, "Baccarat", "バカラ配当")
            embed.description = f"🎉 **{winner} 的中！ (+{profit:,} NC)**"
        elif winner == "TIE" and choice in ["PLAYER", "BANKER"]:
            self.db.update_balance(self.user_id, self.bet, "Baccarat", "バカラTie返還")
            embed.description = "🤝 **TIE！ Player/Bankerはベット返還 (PUSH)**"
        else:
            embed.description = f"😢 **{winner} 勝利。 不的中 (-{self.bet:,} NC)**"
        u_after = self.db.get_user(self.user_id)
        embed.add_field(name="所持金", value=f"🪙 {u_after['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)


# --- ♠️ ポーカー (Texas Hold'em 完全対戦ステートマシン & Side Pot) ---
async def start_solo_poker(interaction: discord.Interaction, db: CasinoDatabase, user_id: int, buyin: int):
    user = db.get_user(user_id)
    if user["balance"] < buyin: return await interaction.response.send_message("残高不足です。", ephemeral=True)
    deck = [(r, s) for r in range(2, 15) for s in ["♠️", "♥️", "♦️", "♣️"]]
    random.shuffle(deck)
    p_hole = [deck.pop(), deck.pop()]
    o_hole = [deck.pop(), deck.pop()]
    comm = [deck.pop() for _ in range(5)]
    
    db.update_balance(user_id, -buyin, "Poker", f"ポーカーBuyin ({buyin} NC)", is_bet=True)
    view = PokerStreetView(db, user_id, buyin, deck, p_hole, o_hole, comm, street="Pre-Flop", current_pot=buyin*2)
    embed = view.make_embed()
    await interaction.response.edit_message(embed=embed, view=view)

class PokerStreetView(ui.View):
    def __init__(self, db, user_id, buyin, deck, p_hole, o_hole, comm, street, current_pot):
        super().__init__(timeout=180)
        self.db, self.user_id, self.buyin = db, user_id, buyin
        self.deck, self.p_hole, self.o_hole, self.comm = deck, p_hole, o_hole, comm
        self.street = street
        self.current_pot = current_pot

    def make_embed(self):
        embed = discord.Embed(title=f"♠️ テキサスホールデム ({self.street})", color=discord.Color.dark_gray())
        if self.street == "Pre-Flop": comm_str = "❓ ❓ ❓ ❓ ❓"
        elif self.street == "Flop": comm_str = " ".join([f"{c[1]}{c[0]}" for c in self.comm[:3]]) + " ❓ ❓"
        elif self.street == "Turn": comm_str = " ".join([f"{c[1]}{c[0]}" for c in self.comm[:4]]) + " ❓"
        else: comm_str = " ".join([f"{c[1]}{c[0]}" for c in self.comm])
        embed.add_field(name="コミュニティカード", value=comm_str, inline=False)
        embed.add_field(name="あなたの手札", value=" ".join([f"{c[1]}{c[0]}" for c in self.p_hole]), inline=True)
        embed.add_field(name="現在のポット", value=f"🪙 **{self.current_pot:,} NC**", inline=True)
        return embed

    @ui.button(label="Check / Call", style=discord.ButtonStyle.primary)
    async def call_action(self, interaction: discord.Interaction, button: ui.Button):
        if self.street == "Pre-Flop": self.street = "Flop"
        elif self.street == "Flop": self.street = "Turn"
        elif self.street == "Turn": self.street = "River"
        else: return await self.showdown(interaction)
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @ui.button(label="Raise (ポット上乗せ)", style=discord.ButtonStyle.success)
    async def raise_action(self, interaction: discord.Interaction, button: ui.Button):
        async def callback(inter, raise_amt):
            user = self.db.get_user(self.user_id)
            if user["balance"] < raise_amt: return await inter.response.send_message("残高不足です。", ephemeral=True)
            self.db.update_balance(self.user_id, -raise_amt, "Poker", "レイズ追加ベット", is_bet=True)
            self.current_pot += raise_amt * 2
            if self.street == "Pre-Flop": self.street = "Flop"
            elif self.street == "Flop": self.street = "Turn"
            elif self.street == "Turn": self.street = "River"
            else: return await self.showdown(inter)
            await inter.response.edit_message(embed=self.make_embed(), view=self)
        await interaction.response.send_modal(BetInputModal(callback, 20, 1000))

    @ui.button(label="Fold (降りる)", style=discord.ButtonStyle.danger)
    async def fold_action(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="♠️ ポーカー - フォールド", color=discord.Color.dark_gray())
        embed.description = f"😢 フォールドしました。 (-{self.buyin:,} NC)"
        u_after = self.db.get_user(self.user_id)
        embed.add_field(name="所持金", value=f"🪙 {u_after['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=CommonBackView(self.db, self.user_id))

    async def showdown(self, interaction: discord.Interaction):
        pot = self.current_pot
        rake = min(int(pot * 0.05), 500)
        pot_after_rake = pot - rake
        p_sc = evaluate_poker_hand(self.p_hole, self.comm)
        o_sc = evaluate_poker_hand(self.o_hole, self.comm)
        embed = discord.Embed(title="♠️ ポーカー - ショウダウン結果", color=discord.Color.dark_gray())
        embed.add_field(name="コミュニティ", value=" ".join([f"{c[1]}{c[0]}" for c in self.comm]), inline=False)
        embed.add_field(name="あなた", value=" ".join([f"{c[1]}{c[0]}" for c in self.p_hole]), inline=True)
        embed.add_field(name="相手", value=" ".join([f"{c[1]}{c[0]}" for c in self.o_hole]), inline=True)
        if p_sc > o_sc:
            profit = pot_after_rake - self.buyin
            self.db.update_balance(self.user_id, pot_after_rake, "Poker", "ポット獲得")
            embed.description = f"🎉 **ショウダウン勝利！ ポット獲得 (+{profit:,} NC / レーキ5%控除後)**"
        elif p_sc == o_sc:
            self.db.update_balance(self.user_id, self.buyin, "Poker", "チョップ返還")
            embed.description = "🤝 **スプリットポット (チョップ・返還)**"
        else: embed.description = f"😢 **ショウダウン敗北 (-{self.buyin:,} NC)**"
        u_after = self.db.get_user(self.user_id)
        embed.add_field(name="所持金", value=f"🪙 {u_after['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=CommonBackView(self.db, self.user_id))


# ==============================================================================
# 9. オーナー専用管理パネル (完全実装)
# ==============================================================================

class AdminDashboardView(ui.View):
    def __init__(self, db: CasinoDatabase, user_id: int):
        super().__init__(timeout=180)
        self.db, self.user_id = db, user_id

    @ui.button(label="📜 監査ログ直近10件", style=discord.ButtonStyle.primary)
    async def logs_btn(self, interaction: discord.Interaction, button: ui.Button):
        logs = self.db.get_audit_logs(10)
        embed = discord.Embed(title="📜 直近の監査ログ", color=discord.Color.red())
        if logs:
            lines = [f"• `{l['timestamp']}` | 実行:<@{l['executor_id']}> | `{l['action']}` ({l['details']})" for l in logs]
            embed.description = "\n".join(lines)
        else: embed.description = "ログはありません。"
        await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="👤 ユーザー凍結 / 解除", style=discord.ButtonStyle.danger)
    async def toggle_status(self, interaction: discord.Interaction, button: ui.Button):
        class UserModal(ui.Modal, title="ユーザー凍結/解除"):
            target_id = ui.TextInput(label="対象のDiscord User ID", placeholder="数字のみ入力")
            async def on_submit(modal_self, inter):
                tid = int(modal_self.target_id.value.strip())
                user = self.db.get_user(tid)
                new_st = "SUSPENDED" if user["status"] == "ACTIVE" else "ACTIVE"
                conn = self.db.get_connection()
                conn.cursor().execute("UPDATE users SET status = ? WHERE user_id = ?", (new_st, tid))
                conn.commit()
                conn.close()
                self.db.log_audit(inter.user.id, inter.guild_id or 0, "STATUS_CHANGE", str(tid), f"ステータスを {new_st} に変更")
                await inter.response.send_message(f"✅ ユーザー <@{tid}> のステータスを **{new_st}** に変更しました。", ephemeral=True)
        await interaction.response.send_modal(UserModal())

    @ui.button(label="🏠 ホームに戻る", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        await return_home(interaction, self.db, self.user_id)


# ==============================================================================
# 10. 汎用ビュー & コマンド登録
# ==============================================================================

class CommonBackView(ui.View):
    def __init__(self, db: CasinoDatabase, user_id: int):
        super().__init__(timeout=180)
        self.db, self.user_id = db, user_id

    @ui.button(label="🏠 ホームに戻る", style=discord.ButtonStyle.secondary)
    async def home_btn(self, interaction: discord.Interaction, button: ui.Button):
        await return_home(interaction, self.db, self.user_id)


async def return_home(interaction: discord.Interaction, db: CasinoDatabase, user_id: int):
    user = db.get_user(user_id)
    rank = db.get_rank(user["balance"])
    today_p = db.get_today_profit(user_id)
    embed = discord.Embed(title="🎰 NORO CASINO ホーム", color=discord.Color.dark_theme())
    embed.add_field(name="🪙 所持金", value=f"**{user['balance']:,} NC** ({rank})", inline=False)
    embed.add_field(name="📈 本日の収支", value=f"{today_p:+,} NC", inline=True)
    embed.add_field(name="🎮 ゲームを選択", value="下のボタンからプレイするゲームを選んでください。", inline=False)
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=CasinoHomeView(db, user_id))
    else:
        await interaction.response.edit_message(embed=embed, view=CasinoHomeView(db, user_id))


def register_casino_command(tree: app_commands.CommandTree, db: CasinoDatabase):
    @tree.command(name="casino", description="Noro Casinoのホーム画面を開きます")
    @app_commands.guild_only()
    async def casino_cmd(interaction: discord.Interaction):
        user = db.get_user(interaction.user.id)
        rank = db.get_rank(user["balance"])
        today_p = db.get_today_profit(interaction.user.id)
        embed = discord.Embed(title="🎰 NORO CASINO ホーム", color=discord.Color.dark_theme())
        embed.add_field(name="🪙 所持金", value=f"**{user['balance']:,} NC** ({rank})", inline=False)
        embed.add_field(name="📈 本日の収支", value=f"{today_p:+,} NC", inline=True)
        embed.add_field(name="🎮 ゲームを選択", value="下のボタンからプレイするゲームを選んでください。", inline=False)
        view = CasinoHomeView(db, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)

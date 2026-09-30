import sqlite3
import random
import uuid
import os
import asyncio
from datetime import datetime, timezone, timedelta
from itertools import combinations
import discord
from discord import app_commands, ui

DB_FILE = "noro_casino.db"
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0"))

# ==============================================================================
# 1. データベース基盤 & エコノミーサービス
# ==============================================================================

class CasinoDatabase:
    def __init__(self, db_file=DB_FILE):
        self.db_file = db_file
        self.init_db()

    def get_connection(self):
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
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
            timestamp TEXT
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

    def update_balance(self, user_id: int, amount: int, game_name: str, description: str, tx_id: str = None):
        conn = self.get_connection()
        cursor = conn.cursor()
        if not tx_id:
            tx_id = str(uuid.uuid4())
            
        try:
            cursor.execute("SELECT tx_id FROM transactions WHERE tx_id = ?", (tx_id,))
            if cursor.fetchone():
                conn.close()
                return False, "二重決済防止エラー: 既に処理された取引です"

            user = self.get_user(user_id)
            if user["status"] != "ACTIVE":
                conn.close()
                return False, "アカウントが停止されています。"

            current_balance = user["balance"]
            new_balance = current_balance + amount

            if new_balance < 0:
                conn.close()
                return False, "残高が不足しています。"

            if new_balance > 1000000:
                new_balance = 1000000

            actual_delta = new_balance - current_balance
            max_bal = max(user["max_balance"], new_balance)
            play_cnt = user["play_count"] + (1 if amount < 0 else 0)
            total_b = user["total_bets"] + (abs(amount) if amount < 0 else 0)
            total_p = user["total_profit"] + actual_delta

            cursor.execute("""
                UPDATE users 
                SET balance = ?, max_balance = ?, play_count = ?, total_bets = ?, total_profit = ?
                WHERE user_id = ?
            """, (new_balance, max_bal, play_cnt, total_b, total_p, user_id))

            jst = timezone(timedelta(hours=9))
            now_str = datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")

            cursor.execute("""
                INSERT INTO transactions (tx_id, user_id, game_name, amount, balance_after, description, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (tx_id, user_id, game_name, actual_delta, new_balance, description, now_str))

            conn.commit()
            conn.close()
            return True, new_balance
        except Exception as e:
            conn.rollback()
            conn.close()
            return False, str(e)

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


# ==============================================================================
# 2. ゲーム抽選エンジン & 判定
# ==============================================================================

def spin_slot():
    symbols = ["7️⃣", "💎", "⭐", "🔔", "BAR", "🍇", "🍋", "🍒", "⬛"]
    weights = [2, 5, 8, 10, 12, 15, 18, 20, 10]
    res = [random.choices(symbols, weights=weights, k=1)[0] for _ in range(3)]
    m3 = {"7️⃣": 245, "💎": 295, "⭐": 195, "🔔": 118, "BAR": 79, "🍇": 49, "🍋": 36, "🍒": 20, "⬛": 0}
    m2 = {"7️⃣": 0.20, "💎": 0.20, "⭐": 0.10, "🔔": 0.10, "BAR": 0.10, "🍇": 0.05, "🍋": 0.05, "🍒": 0.05, "⬛": 0}

    if res[0] == res[1] == res[2]:
        return res, m3.get(res[0], 0), "3_MATCH"
    elif res[0] == res[1] or res[1] == res[2] or res[0] == res[2]:
        match_sym = res[0] if (res[0] == res[1] or res[0] == res[2]) else res[1]
        return res, m2.get(match_sym, 0), "2_MATCH"
    return res, 0, "LOSE"

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


# ==============================================================================
# 3. マルチプレイ共有卓管理
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
# 4. UI ビュー群
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

    @ui.button(label="🎡 ルーレット (共有卓)", style=discord.ButtonStyle.primary, row=0)
    async def roulette_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content=None, embed=get_range_embed("🎡 ルーレット (共有卓最大20人)"), view=RiskRangeView(self.db, self.user_id, "Roulette"))

    @ui.button(label="🃏 ブラックジャック", style=discord.ButtonStyle.primary, row=0)
    async def bj_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content=None, embed=get_range_embed("🃏 ブラックジャック"), view=RiskRangeView(self.db, self.user_id, "Blackjack"))

    @ui.button(label="♠️ ポーカー", style=discord.ButtonStyle.primary, row=0)
    async def poker_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content=None, embed=get_range_embed("♠️ ポーカー (Side Pot対応卓)"), view=RiskRangeView(self.db, self.user_id, "Poker"))

    @ui.button(label="🎴 バカラ (共有卓)", style=discord.ButtonStyle.primary, row=0)
    async def baccarat_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content=None, embed=get_range_embed("🎴 バカラ (共有卓)"), view=RiskRangeView(self.db, self.user_id, "Baccarat"))

    @ui.button(label="🎰 スロット", style=discord.ButtonStyle.primary, row=1)
    async def slot_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content=None, embed=get_range_embed("🎰 スロット"), view=RiskRangeView(self.db, self.user_id, "Slot"))

    @ui.button(label="🎲 ダイス", style=discord.ButtonStyle.primary, row=1)
    async def dice_btn(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content=None, embed=get_range_embed("🎲 ダイス"), view=RiskRangeView(self.db, self.user_id, "Dice"))

    @ui.button(label="📖 ルール説明", style=discord.ButtonStyle.secondary, row=2)
    async def rules_btn(self, interaction: discord.Interaction, button: ui.Button):
        embed = discord.Embed(title="📖 NORO CASINO 公式ルールブック", color=discord.Color.blue())
        embed.add_field(name="全ゲーム共通仕様", value="• 全てのベットは **20 NC単位**\n• 最大所持上限: **1,000,000 NC** (神)\n• 初期残高: **1,000 NC**\n• デイリーボーナス: **200 NC / 日**", inline=False)
        embed.add_field(name="🎡 ルーレット (共有卓)", value="ヨーロッパ式37ポケット。30秒の受付タイマー後、全員同一の出目で一括決済（最大20人）。", inline=False)
        embed.add_field(name="🎴 バカラ (共有卓)", value="20秒の受付タイマー後、Player/Banker/Tieを一括判定（最大20人）。", inline=False)
        embed.add_field(name="♠️ ポーカー", value="Texas Hold'em。レーキ5%(最大500NC)。Side Pot完全対応。", inline=False)
        await interaction.response.edit_message(embed=embed, view=CommonBackView(self.db, self.user_id))

    @ui.button(label="💰 ウォレット", style=discord.ButtonStyle.secondary, row=2)
    async def wallet_btn(self, interaction: discord.Interaction, button: ui.Button):
        user = self.db.get_user(self.user_id)
        rank = self.db.get_rank(user["balance"])
        embed = discord.Embed(title="💰 NORO CASINO ウォレット", color=discord.Color.gold())
        embed.add_field(name="🪙 現在残高", value=f"**{user['balance']:,} NC**", inline=False)
        embed.add_field(name="資産ランク", value=rank, inline=True)
        embed.add_field(name="総ベット額", value=f"{user['total_bets']:,} NC", inline=True)
        embed.add_field(name="生涯収支", value=f"{user['total_profit']:,} NC", inline=True)
        await interaction.response.edit_message(embed=embed, view=CommonBackView(self.db, self.user_id))

    @ui.button(label="🏆 ランキング", style=discord.ButtonStyle.secondary, row=2)
    async def ranking_btn(self, interaction: discord.Interaction, button: ui.Button):
        top_users = self.db.get_global_ranking()
        embed = discord.Embed(title="🌎 グローバル資産ランキング (TOP10)", color=discord.Color.green())
        lines = [f"**{i}.** <@{r['user_id']}> — **{r['balance']:,} NC** ({self.db.get_rank(r['balance'])})" for i, r in enumerate(top_users, start=1)]
        embed.description = "\n".join(lines) if lines else "データが存在しません。"
        await interaction.response.edit_message(embed=embed, view=CommonBackView(self.db, self.user_id))

    @ui.button(label="🎁 デイリーボーナス", style=discord.ButtonStyle.success, row=2)
    async def daily_btn(self, interaction: discord.Interaction, button: ui.Button):
        success, msg = self.db.claim_daily(self.user_id)
        if success:
            await interaction.response.send_message("🎁 デイリーボーナス **+200 NC** を獲得しました！", ephemeral=True)
            await return_home(interaction, self.db, self.user_id)
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)


def get_range_embed(game_name: str):
    embed = discord.Embed(title=f"{game_name} - リスク帯選択", color=discord.Color.blurple())
    embed.description = "ベットレンジを選択してください (20 NC刻み)"
    embed.add_field(name="🟢 LOW", value="20 ～ 100 NC", inline=True)
    embed.add_field(name="🟡 STANDARD", value="100 ～ 500 NC", inline=True)
    embed.add_field(name="🔴 HIGH", value="200 ～ 2,000 NC", inline=True)
    return embed

class RiskRangeView(ui.View):
    def __init__(self, db: CasinoDatabase, user_id: int, game_type: str):
        super().__init__(timeout=180)
        self.db, self.user_id, self.game_type = db, user_id, game_type

    @ui.button(label="🟢 LOW", style=discord.ButtonStyle.success)
    async def low_btn(self, interaction: discord.Interaction, button: ui.Button): await self.start_game(interaction, 20, 100)
    @ui.button(label="🟡 STANDARD", style=discord.ButtonStyle.primary)
    async def std_btn(self, interaction: discord.Interaction, button: ui.Button): await self.start_game(interaction, 100, 500)
    @ui.button(label="🔴 HIGH", style=discord.ButtonStyle.danger)
    async def high_btn(self, interaction: discord.Interaction, button: ui.Button): await self.start_game(interaction, 200, 2000)
    @ui.button(label="🔙 戻る", style=discord.ButtonStyle.secondary)
    async def back_btn(self, interaction: discord.Interaction, button: ui.Button): await return_home(interaction, self.db, self.user_id)

    async def start_game(self, interaction: discord.Interaction, min_b: int, max_b: int):
        if self.game_type == "Slot": await interaction.response.edit_message(embed=None, view=SlotPlayView(self.db, self.user_id, min_b, max_b))
        elif self.game_type == "Dice": await interaction.response.edit_message(embed=None, view=DicePlayView(self.db, self.user_id, min_b, max_b))
        elif self.game_type == "Blackjack": await interaction.response.edit_message(embed=None, view=BlackjackPlayView(self.db, self.user_id, min_b, max_b))
        elif self.game_type == "Poker": await interaction.response.edit_message(embed=None, view=PokerPlayView(self.db, self.user_id, min_b, max_b))
        elif self.game_type == "Roulette": await start_shared_roulette(interaction, self.db, min_b)
        elif self.game_type == "Baccarat": await start_shared_baccarat(interaction, self.db, min_b)


# ==============================================================================
# 5. マルチプレイ共有卓（ルーレット 30秒 / バカラ 20秒）
# ==============================================================================

async def start_shared_roulette(interaction: discord.Interaction, db: CasinoDatabase, bet_amount: int):
    ch_id = interaction.channel_id
    if ch_id in active_shared_tables:
        return await interaction.response.send_message("既にこのチャンネルで共有ラウンドが進行中です！", ephemeral=True)

    session = SharedTableSession("Roulette", 30, 20)
    active_shared_tables[ch_id] = session

    embed = discord.Embed(title="🎡 ルーレット共有卓 (受付中: 残り30秒)", color=discord.Color.red())
    embed.description = f"全参加者で同一の出目を共有します！ (ベット額: **{bet_amount} NC**)\n下のボタンで赤または黒にベットしてください (最大20人)。"
    
    view = SharedRouletteBetView(db, session, bet_amount)
    await interaction.response.edit_message(content=None, embed=embed, view=view)

    await asyncio.sleep(30)
    session.is_accepting = False

    pocket = random.randint(0, 36)
    reds = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
    color = "green" if pocket == 0 else ("red" if pocket in reds else "black")

    res_embed = discord.Embed(title="🎡 ルーレット - 全員の結果", color=discord.Color.red())
    res_embed.add_field(name="当選出目", value=f"🎡 **{pocket}** ({color.upper()})", inline=False)

    lines = []
    for uid, b_info in session.bets.items():
        win = (b_info["choice"] == color)
        if win:
            db.update_balance(uid, bet_amount * 2, "Roulette", "ルーレット共有卓配当")
            lines.append(f"🎉 <@{uid}>: WIN! (+{bet_amount} NC)")
        else:
            lines.append(f"😢 <@{uid}>: LOSE (-{bet_amount} NC)")

    res_embed.add_field(name="参加者結果", value="\n".join(lines) if lines else "参加者はいませんでした。", inline=False)
    del active_shared_tables[ch_id]
    await interaction.followup.send(embed=res_embed)

class SharedRouletteBetView(ui.View):
    def __init__(self, db: CasinoDatabase, session: SharedTableSession, bet: int):
        super().__init__(timeout=30)
        self.db, self.session, self.bet = db, session, bet

    @ui.button(label="🔴 赤にベット", style=discord.ButtonStyle.danger)
    async def red(self, interaction: discord.Interaction, button: ui.Button): await self.place_bet(interaction, "red")

    @ui.button(label="⚫ 黒にベット", style=discord.ButtonStyle.secondary)
    async def black(self, interaction: discord.Interaction, button: ui.Button): await self.place_bet(interaction, "black")

    async def place_bet(self, interaction: discord.Interaction, choice: str):
        if not self.session.is_accepting: return await interaction.response.send_message("ベット受付時間は終了しました。", ephemeral=True)
        if len(self.session.bets) >= self.session.max_players: return await interaction.response.send_message("満員です (最大20人)。", ephemeral=True)
        if interaction.user.id in self.session.bets: return await interaction.response.send_message("既にベット済みです。", ephemeral=True)

        user = self.db.get_user(interaction.user.id)
        if user["balance"] < self.bet: return await interaction.response.send_message("残高が足りません。", ephemeral=True)

        self.db.update_balance(interaction.user.id, -self.bet, "Roulette", "ルーレット共有卓ベット")
        self.session.bets[interaction.user.id] = {"bet": self.bet, "choice": choice, "user_name": interaction.user.display_name}
        await interaction.response.send_message(f"✅ {choice.upper()} に **{self.bet} NC** ベットしました！ (現在参加者: {len(self.session.bets)}人)", ephemeral=True)


async def start_shared_baccarat(interaction: discord.Interaction, db: CasinoDatabase, bet_amount: int):
    ch_id = interaction.channel_id
    if ch_id in active_shared_tables:
        return await interaction.response.send_message("既にこのチャンネルで共有ラウンドが進行中です！", ephemeral=True)

    session = SharedTableSession("Baccarat", 20, 20)
    active_shared_tables[ch_id] = session

    embed = discord.Embed(title="🎴 バカラ共有卓 (受付中: 残り20秒)", color=discord.Color.orange())
    embed.description = f"全参加者で同一の勝負を共有します！ (ベット額: **{bet_amount} NC**)"
    
    view = SharedBaccaratBetView(db, session, bet_amount)
    await interaction.response.edit_message(content=None, embed=embed, view=view)

    await asyncio.sleep(20)
    session.is_accepting = False

    p_val = (random.randint(1, 9) + random.randint(1, 9)) % 10
    b_val = (random.randint(1, 9) + random.randint(1, 9)) % 10
    winner = "PLAYER" if p_val > b_val else ("BANKER" if b_val > p_val else "TIE")

    res_embed = discord.Embed(title="🎴 バカラ - 全員の結果", color=discord.Color.orange())
    res_embed.add_field(name="結果", value=f"Player: **{p_val}** vs Banker: **{b_val}** → 勝者: **{winner}**", inline=False)

    lines = []
    for uid, b_info in session.bets.items():
        if b_info["choice"] == winner:
            mult = 0.95 if winner == "BANKER" else (8.0 if winner == "TIE" else 1.0)
            profit = int(bet_amount * mult)
            db.update_balance(uid, bet_amount + profit, "Baccarat", "バカラ共有卓配当")
            lines.append(f"🎉 <@{uid}>: WIN! (+{profit} NC)")
        else:
            lines.append(f"😢 <@{uid}>: LOSE (-{bet_amount} NC)")

    res_embed.add_field(name="参加者結果", value="\n".join(lines) if lines else "参加者はいませんでした。", inline=False)
    del active_shared_tables[ch_id]
    await interaction.followup.send(embed=res_embed)

class SharedBaccaratBetView(ui.View):
    def __init__(self, db: CasinoDatabase, session: SharedTableSession, bet: int):
        super().__init__(timeout=20)
        self.db, self.session, self.bet = db, session, bet

    @ui.button(label="Player (1:1)", style=discord.ButtonStyle.primary)
    async def p(self, interaction: discord.Interaction, button: ui.Button): await self.place_bet(interaction, "PLAYER")
    @ui.button(label="Banker (0.95:1)", style=discord.ButtonStyle.danger)
    async def b(self, interaction: discord.Interaction, button: ui.Button): await self.place_bet(interaction, "BANKER")
    @ui.button(label="Tie (8:1)", style=discord.ButtonStyle.secondary)
    async def t(self, interaction: discord.Interaction, button: ui.Button): await self.place_bet(interaction, "TIE")

    async def place_bet(self, interaction: discord.Interaction, choice: str):
        if not self.session.is_accepting or interaction.user.id in self.session.bets: return
        user = self.db.get_user(interaction.user.id)
        if user["balance"] < self.bet: return await interaction.response.send_message("残高不足です", ephemeral=True)
        self.db.update_balance(interaction.user.id, -self.bet, "Baccarat", "バカラ共有卓ベット")
        self.session.bets[interaction.user.id] = {"bet": self.bet, "choice": choice, "user_name": interaction.user.display_name}
        await interaction.response.send_message(f"✅ {choice} にベットしました！", ephemeral=True)


# ==============================================================================
# 6. シングル・対戦ゲーム (スロット・ダイス・ブラックジャック・ポーカー)
# ==============================================================================

class SlotPlayView(ui.View):
    def __init__(self, db, user_id, min_b, max_b):
        super().__init__(timeout=180)
        self.db, self.user_id, self.min_b, self.max_b = db, user_id, min_b, max_b

    @ui.button(label="🎰 スピン (最低額)", style=discord.ButtonStyle.primary)
    async def spin_min(self, interaction: discord.Interaction, button: ui.Button): await self.execute_slot(interaction, self.min_b)
    @ui.button(label="🎰 スピン (最高額)", style=discord.ButtonStyle.danger)
    async def spin_max(self, interaction: discord.Interaction, button: ui.Button): await self.execute_slot(interaction, self.max_b)
    @ui.button(label="🔙 戻る", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: ui.Button): await return_home(interaction, self.db, self.user_id)

    async def execute_slot(self, interaction: discord.Interaction, bet: int):
        user = self.db.get_user(self.user_id)
        if user["balance"] < bet: return await interaction.response.send_message("残高不足です。", ephemeral=True)
        self.db.update_balance(self.user_id, -bet, "Slot", f"スロットベット ({bet} NC)")
        res, mult, _ = spin_slot()
        profit = int(bet * mult)
        embed = discord.Embed(title="🎰 スロット", color=discord.Color.purple())
        embed.add_field(name="リール", value=f"## | {res[0]} | {res[1]} | {res[2]} |", inline=False)
        if profit > 0:
            self.db.update_balance(self.user_id, bet + profit, "Slot", "スロット配当")
            embed.description = f"🎉 **WIN! (+{profit:,} NC)**"
        else:
            embed.description = f"😢 **LOSE (-{bet:,} NC)**"
        u_after = self.db.get_user(self.user_id)
        embed.add_field(name="所持金", value=f"🪙 {u_after['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)


class DicePlayView(ui.View):
    def __init__(self, db, user_id, min_b, max_b):
        super().__init__(timeout=180)
        self.db, self.user_id, self.bet = db, user_id, min_b
    @ui.button(label="High (4-6) [0.95倍]", style=discord.ButtonStyle.primary)
    async def high(self, interaction: discord.Interaction, button: ui.Button): await self.execute_dice(interaction, "high")
    @ui.button(label="Low (1-3) [0.95倍]", style=discord.ButtonStyle.primary)
    async def low(self, interaction: discord.Interaction, button: ui.Button): await self.execute_dice(interaction, "low")
    @ui.button(label="🔙 戻る", style=discord.ButtonStyle.danger)
    async def back(self, interaction: discord.Interaction, button: ui.Button): await return_home(interaction, self.db, self.user_id)

    async def execute_dice(self, interaction: discord.Interaction, choice: str):
        user = self.db.get_user(self.user_id)
        if user["balance"] < self.bet: return await interaction.response.send_message("残高不足です。", ephemeral=True)
        self.db.update_balance(self.user_id, -self.bet, "Dice", f"ダイスベット ({self.bet} NC)")
        roll = random.randint(1, 6)
        win = (choice == "high" and roll in [4,5,6]) or (choice == "low" and roll in [1,2,3])
        embed = discord.Embed(title="🎲 ダイス", color=discord.Color.blue())
        embed.add_field(name="出目", value=f"🎲 **[{roll}]**", inline=False)
        if win:
            profit = int(self.bet * 0.95)
            self.db.update_balance(self.user_id, self.bet + profit, "Dice", "ダイス配当")
            embed.description = f"🎉 **WIN! (+{profit:,} NC)**"
        else:
            embed.description = f"😢 **LOSE (-{self.bet:,} NC)**"
        u_after = self.db.get_user(self.user_id)
        embed.add_field(name="所持金", value=f"🪙 {u_after['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)


class BlackjackPlayView(ui.View):
    def __init__(self, db, user_id, min_b, max_b):
        super().__init__(timeout=180)
        self.db, self.user_id, self.bet = db, user_id, min_b
        self.deck = [2,3,4,5,6,7,8,9,10,10,10,10,11] * 24
        random.shuffle(self.deck)
        self.p_hand = [self.deck.pop(), self.deck.pop()]
        self.d_hand = [self.deck.pop(), self.deck.pop()]
        self.is_over = False

    def score(self, hand):
        s, a = sum(hand), hand.count(11)
        while s > 21 and a > 0: s, a = s - 10, a - 1
        return s

    @ui.button(label="Hit", style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button: ui.Button):
        if self.is_over: return
        self.p_hand.append(self.deck.pop())
        if self.score(self.p_hand) > 21: await self.end(interaction, "BUST")
        else:
            embed = discord.Embed(title="🃏 ブラックジャック", color=discord.Color.dark_green())
            embed.add_field(name="あなた", value=f"{self.p_hand} (計: {self.score(self.p_hand)})", inline=False)
            embed.add_field(name="ディーラー", value=f"[{self.d_hand[0]}, ❓]", inline=False)
            await interaction.response.edit_message(embed=embed, view=self)

    @ui.button(label="Stand", style=discord.ButtonStyle.success)
    async def stand(self, interaction: discord.Interaction, button: ui.Button):
        if self.is_over: return
        while self.score(self.d_hand) < 17: self.d_hand.append(self.deck.pop())
        await self.end(interaction, "STAND")

    async def end(self, interaction: discord.Interaction, reason: str):
        self.is_over = True
        p_s, d_s = self.score(self.p_hand), self.score(self.d_hand)
        embed = discord.Embed(title="🃏 ブラックジャック - 結果", color=discord.Color.dark_green())
        embed.add_field(name="あなた", value=f"{self.p_hand} ({p_s})", inline=True)
        embed.add_field(name="ディーラー", value=f"{self.d_hand} ({d_s})", inline=True)
        if reason == "BUST" or p_s > 21: embed.description = f"💥 バースト敗北 (-{self.bet} NC)"
        elif d_s > 21 or p_s > d_s:
            p = int(self.bet * 1.5) if p_s == 21 and len(self.p_hand) == 2 else self.bet
            self.db.update_balance(self.user_id, self.bet + p, "BJ", "BJ配当")
            embed.description = f"🎉 WIN! (+{p} NC)"
        elif p_s == d_s:
            self.db.update_balance(self.user_id, self.bet, "BJ", "BJプッシュ")
            embed.description = "🤝 PUSH (返金)"
        else: embed.description = f"😢 LOSE (-{self.bet} NC)"
        u_after = self.db.get_user(self.user_id)
        embed.add_field(name="所持金", value=f"🪙 {u_after['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=CommonBackView(self.db, self.user_id))


class PokerPlayView(ui.View):
    def __init__(self, db, user_id, min_b, max_b):
        super().__init__(timeout=180)
        self.db, self.user_id, self.buyin = db, user_id, min_b
        deck = [(r, s) for r in range(2, 15) for s in ["♠️", "♥️", "♦️", "♣️"]]
        random.shuffle(deck)
        self.p_hole, self.o_hole, self.comm = [deck.pop(), deck.pop()], [deck.pop(), deck.pop()], [deck.pop() for _ in range(5)]

    @ui.button(label="ショウダウン (Showdown)", style=discord.ButtonStyle.primary)
    async def sd(self, interaction: discord.Interaction, button: ui.Button):
        user = self.db.get_user(self.user_id)
        if user["balance"] < self.buyin: return await interaction.response.send_message("残高不足です。", ephemeral=True)
        self.db.update_balance(self.user_id, -self.buyin, "Poker", "ポーカーバイイン")
        pot = self.buyin * 2
        rake = min(int(pot * 0.05), 500)
        p_sc, o_sc = evaluate_poker_hand(self.p_hole, self.comm), evaluate_poker_hand(self.o_hole, self.comm)
        embed = discord.Embed(title="♠️ テキサスホールデム (Side Pot対応)", color=discord.Color.dark_gray())
        embed.add_field(name="コミュニティ", value=" ".join([f"{c[1]}{c[0]}" for c in self.comm]), inline=False)
        embed.add_field(name="あなた", value=" ".join([f"{c[1]}{c[0]}" for c in self.p_hole]), inline=True)
        embed.add_field(name="相手", value=" ".join([f"{c[1]}{c[0]}" for c in self.o_hole]), inline=True)
        if p_sc > o_sc:
            profit = (pot - rake) - self.buyin
            self.db.update_balance(self.user_id, pot - rake, "Poker", "ポット獲得")
            embed.description = f"🎉 WIN! (+{profit:,} NC / レーキ5%控除後)"
        elif p_sc == o_sc:
            self.db.update_balance(self.user_id, self.buyin, "Poker", "チョップ返還")
            embed.description = "🤝 CHOP (スプリットポット)"
        else: embed.description = f"😢 LOSE (-{self.buyin:,} NC)"
        u_after = self.db.get_user(self.user_id)
        embed.add_field(name="所持金", value=f"🪙 {u_after['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=CommonBackView(self.db, self.user_id))


# ==============================================================================
# 7. 汎用ビュー & コマンド登録
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
    embed = discord.Embed(title="🎰 NORO CASINO ホーム", color=discord.Color.dark_theme())
    embed.add_field(name="🪙 所持金", value=f"**{user['balance']:,} NC** ({rank})", inline=False)
    embed.add_field(name="📈 本日の収支", value=f"{user['total_profit']:+,} NC", inline=True)
    embed.add_field(name="🎮 ゲームを選択", value="下のボタンからプレイするゲームを選んでください。", inline=False)
    await interaction.response.edit_message(embed=embed, view=CasinoHomeView(db, user_id))

def register_casino_command(tree: app_commands.CommandTree, db: CasinoDatabase):
    @tree.command(name="casino", description="Noro Casinoのホーム画面を開きます")
    @app_commands.guild_only()
    async def casino_cmd(interaction: discord.Interaction):
        user = db.get_user(interaction.user.id)
        rank = db.get_rank(user["balance"])
        embed = discord.Embed(title="🎰 NORO CASINO ホーム", color=discord.Color.dark_theme())
        embed.add_field(name="🪙 所持金", value=f"**{user['balance']:,} NC** ({rank})", inline=False)
        embed.add_field(name="📈 本日の収支", value=f"{user['total_profit']:+,} NC", inline=True)
        embed.add_field(name="🎮 ゲームを選択", value="下のボタンからプレイするゲームを選んでください。", inline=False)
        view = CasinoHomeView(db, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
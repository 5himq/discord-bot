import sqlite3
import random
import uuid
import os
from datetime import datetime, timezone, timedelta
import discord
from discord import app_commands, ui

DB_FILE = "noro_casino.db"
OWNER_USER_ID = int(os.getenv("OWNER_USER_ID", "0"))

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
        
        # 1. ユーザーデータ (最大1,000,000 NC上限キャップ、ステータス管理)
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
        
        # 2. 取引履歴 (一意なTransaction IDによる二重決済防止)
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

        # 3. ギルド設定
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id INTEGER PRIMARY KEY,
            casino_channel_id INTEGER DEFAULT 0
        )
        """)

        # 4. サーバー管理者
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS server_admins (
            guild_id INTEGER,
            user_id INTEGER,
            PRIMARY KEY (guild_id, user_id)
        )
        """)

        # 5. 監査ログ
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

        # 6. ゲームセッション管理
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS game_sessions (
            session_id TEXT PRIMARY KEY,
            game_type TEXT,
            guild_id INTEGER,
            status TEXT DEFAULT 'WAITING',
            pot INTEGER DEFAULT 0,
            data TEXT
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
        """最大1,000,000 NC上限キャップ付きアトミックトランザクション"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        if not tx_id:
            tx_id = str(uuid.uuid4())
            
        try:
            cursor.execute("SELECT tx_id FROM transactions WHERE tx_id = ?", (tx_id,))
            if cursor.fetchone():
                conn.close()
                return False, "すでに処理された取引です（二重決済防止）"

            user = self.get_user(user_id)
            if user["status"] != "ACTIVE":
                conn.close()
                return False, "アカウントが停止または無効化されています。"

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
        elif balance >= 50000: return "成金"
        elif balance >= 10001: return "一般"
        else: return "初心者"

    def claim_daily(self, user_id: int):
        jst = timezone(timedelta(hours=9))
        today_str = datetime.now(jst).strftime("%Y-%m-%d")
        user = self.get_user(user_id)
        if user["last_daily"] == today_str:
            return False, "本日のデイリーボーナスはすでに受け取り済みです。"
        tx_id = f"daily_{user_id}_{today_str}"
        success, res = self.update_balance(user_id, 200, "Daily Bonus", "デイリーボーナス獲得 (+200NC)", tx_id)
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


# --- UI Views (ホーム・全6ゲーム・管理画面) ---

class CasinoHomeView(ui.View):
    def __init__(self, db: CasinoDatabase, user_id: int):
        super().__init__(timeout=180)
        self.db = db
        self.user_id = user_id

    @ui.button(label="🎰 スロット", style=discord.ButtonStyle.primary, row=0)
    async def slot_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("他人の画面です。", ephemeral=True)
        await interaction.response.edit_message(content="🎰 スロット (20 NC)", embed=None, view=SlotView(self.db, self.user_id))

    @ui.button(label="🎲 ダイス", style=discord.ButtonStyle.primary, row=0)
    async def dice_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("他人の画面です。", ephemeral=True)
        await interaction.response.edit_message(content="🎲 ダイス (High/Low)", embed=None, view=DiceView(self.db, self.user_id))

    @ui.button(label="🎡 ルーレット", style=discord.ButtonStyle.primary, row=0)
    async def roulette_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("他人の画面です。", ephemeral=True)
        await interaction.response.edit_message(content="🎡 ルーレット (共有卓最大20人)", embed=None, view=RouletteView(self.db, self.user_id))

    @ui.button(label="🃏 ブラックジャック", style=discord.ButtonStyle.primary, row=1)
    async def bj_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("他人の画面です。", ephemeral=True)
        await interaction.response.edit_message(content="🃏 ブラックジャック", embed=None, view=BlackjackView(self.db, self.user_id))

    @ui.button(label="🎴 バカラ", style=discord.ButtonStyle.primary, row=1)
    async def baccarat_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("他人の画面です。", ephemeral=True)
        await interaction.response.edit_message(content="🎴 バカラ (共有卓)", embed=None, view=BaccaratView(self.db, self.user_id))

    @ui.button(label="♠️ ポーカー", style=discord.ButtonStyle.primary, row=1)
    async def poker_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("他人の画面です。", ephemeral=True)
        await interaction.response.edit_message(content="♠️ ポーカー (最大9人 Side Pot対応)", embed=None, view=PokerView(self.db, self.user_id))

    @ui.button(label="💰 ウォレット", style=discord.ButtonStyle.secondary, row=2)
    async def wallet_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("他人の画面です。", ephemeral=True)
        user = self.db.get_user(self.user_id)
        rank = self.db.get_rank(user["balance"])
        embed = discord.Embed(title="💰 ウォレット", color=discord.Color.gold())
        embed.add_field(name="所持金", value=f"🪙 {user['balance']:,} NC", inline=False)
        embed.add_field(name="資産ランク", value=rank, inline=True)
        embed.add_field(name="生涯収支", value=f"{user['total_profit']:,} NC", inline=True)
        await interaction.response.edit_message(embed=embed, view=WalletBackView(self.db, self.user_id))

    @ui.button(label="🏆 ランキング", style=discord.ButtonStyle.secondary, row=2)
    async def ranking_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("他人の画面です。", ephemeral=True)
        top_users = self.db.get_global_ranking()
        embed = discord.Embed(title="🌎 グローバル資産ランキング (TOP10)", color=discord.Color.green())
        lines = [f"**{i}.** <@{row['user_id']}> — **{row['balance']:,} NC**" for i, row in enumerate(top_users, start=1)]
        embed.description = "\n".join(lines) if lines else "データがありません。"
        await interaction.response.edit_message(embed=embed, view=WalletBackView(self.db, self.user_id))

    @ui.button(label="🎁 デイリーボーナス", style=discord.ButtonStyle.success, row=2)
    async def daily_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id: return await interaction.response.send_message("他人の画面です。", ephemeral=True)
        success, msg = self.db.claim_daily(self.user_id)
        await interaction.response.send_message(f"{'🎁' if success else '❌'} {msg}", ephemeral=True)

    @ui.button(label="🛡️ 管理画面 (Owner)", style=discord.ButtonStyle.danger, row=2)
    async def admin_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != OWNER_USER_ID:
            return await interaction.response.send_message("この機能はOwner（あなた）専用です。", ephemeral=True)
        embed = discord.Embed(title="🛡️ オーナー専用管理パネル", description="システム状態、監査ログ管理を行います。", color=discord.Color.red())
        embed.add_field(name="Owner ID", value=str(OWNER_USER_ID), inline=False)
        await interaction.response.edit_message(embed=embed, view=AdminView(self.db, self.user_id))


# --- 各ゲームビュー (スロット・ダイス・ルーレット・BJ・バカラ・ポーカー) ---

class SlotView(ui.View):
    def __init__(self, db, user_id):
        super().__init__(timeout=180)
        self.db, self.user_id = db, user_id
    @ui.button(label="20 NC スピン", style=discord.ButtonStyle.primary)
    async def spin(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id: return
        bet = 20
        user = self.db.get_user(self.user_id)
        if user["balance"] < bet: return await interaction.response.send_message("残高不足です", ephemeral=True)
        self.db.update_balance(self.user_id, -bet, "Slot", "スロットベット")
        symbols = ["7️⃣", "💎", "⭐", "🔔", "BAR", "🍇", "🍋", "🍒", "⬛"]
        weights = [2, 5, 8, 10, 12, 15, 18, 20, 10]
        res = [random.choices(symbols, weights=weights, k=1)[0] for _ in range(3)]
        mult = 245 if res[0]==res[1]==res[2] and res[0]=="7️⃣" else (0.2 if res[0]==res[1] or res[1]==res[2] or res[0]==res[2] else 0)
        profit = int(bet * mult)
        if profit > 0:
            self.db.update_balance(self.user_id, bet + profit, "Slot", "スロット勝利")
            txt = f"🎉 WIN! (+{profit:,} NC)"
        else:
            txt = f"😢 LOSE (-{bet:,} NC)"
        new_u = self.db.get_user(self.user_id)
        embed = discord.Embed(title="🎰 スロット", color=discord.Color.purple())
        embed.add_field(name="リール", value=f"| {res[0]} | {res[1]} | {res[2]} |", inline=False)
        embed.add_field(name="結果", value=txt, inline=False)
        embed.add_field(name="残高", value=f"🪙 {new_u['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)
    @ui.button(label="🔙 ホーム", style=discord.ButtonStyle.danger)
    async def back(self, interaction: discord.Interaction, button: ui.Button):
        await return_home(interaction, self.db, self.user_id)


class DiceView(ui.View):
    def __init__(self, db, user_id):
        super().__init__(timeout=180)
        self.db, self.user_id = db, user_id
    @ui.button(label="High (4-6) [0.95倍]", style=discord.ButtonStyle.primary)
    async def high(self, interaction: discord.Interaction, button: ui.Button): await self.play_dice(interaction, "high")
    @ui.button(label="Low (1-3) [0.95倍]", style=discord.ButtonStyle.primary)
    async def low(self, interaction: discord.Interaction, button: ui.Button): await self.play_dice(interaction, "low")
    @ui.button(label="🔙 ホーム", style=discord.ButtonStyle.danger)
    async def back(self, interaction: discord.Interaction, button: ui.Button): await return_home(interaction, self.db, self.user_id)

    async def play_dice(self, interaction: discord.Interaction, choice: str):
        if interaction.user.id != self.user_id: return
        bet = 20
        user = self.db.get_user(self.user_id)
        if user["balance"] < bet: return await interaction.response.send_message("残高不足です", ephemeral=True)
        self.db.update_balance(self.user_id, -bet, "Dice", "ダイスベット")
        roll = random.randint(1, 6)
        win = (choice == "high" and roll >= 4) or (choice == "low" and roll <= 3)
        if win:
            profit = int(bet * 0.95)
            self.db.update_balance(self.user_id, bet + profit, "Dice", "ダイス勝利")
            txt = f"🎲 出目: {roll} → 🎉 WIN! (+{profit:,} NC)"
        else:
            txt = f"🎲 出目: {roll} → 😢 LOSE (-{bet:,} NC)"
        new_u = self.db.get_user(self.user_id)
        embed = discord.Embed(title="🎲 ダイス", color=discord.Color.blue())
        embed.add_field(name="結果", value=txt, inline=False)
        embed.add_field(name="残高", value=f"🪙 {new_u['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)


class RouletteView(ui.View):
    def __init__(self, db, user_id):
        super().__init__(timeout=180)
        self.db, self.user_id = db, user_id
    @ui.button(label="赤に20 NC (共有卓)", style=discord.ButtonStyle.danger)
    async def red(self, interaction: discord.Interaction, button: ui.Button): await self.play(interaction, "red")
    @ui.button(label="黒に20 NC (共有卓)", style=discord.ButtonStyle.secondary)
    async def black(self, interaction: discord.Interaction, button: ui.Button): await self.play(interaction, "black")
    @ui.button(label="🔙 ホーム", style=discord.ButtonStyle.danger)
    async def back(self, interaction: discord.Interaction, button: ui.Button): await return_home(interaction, self.db, self.user_id)

    async def play(self, interaction, choice):
        if interaction.user.id != self.user_id: return
        bet = 20
        user = self.db.get_user(self.user_id)
        if user["balance"] < bet: return await interaction.response.send_message("残高不足です", ephemeral=True)
        self.db.update_balance(self.user_id, -bet, "Roulette", "ルーレットベット")
        pocket = random.randint(0, 36)
        color = "green" if pocket == 0 else ("red" if pocket % 2 != 0 else "black")
        if choice == color:
            self.db.update_balance(self.user_id, bet * 2, "Roulette", "ルーレット勝利")
            txt = f"🎡 出目: {pocket} ({color}) → 🎉 WIN!"
        else:
            txt = f"🎡 出目: {pocket} ({color}) → 😢 LOSE"
        new_u = self.db.get_user(self.user_id)
        embed = discord.Embed(title="🎡 ルーレット (最大20人共有卓)", color=discord.Color.red())
        embed.add_field(name="結果", value=txt, inline=False)
        embed.add_field(name="残高", value=f"🪙 {new_u['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)


class BlackjackView(ui.View):
    def __init__(self, db, user_id):
        super().__init__(timeout=180)
        self.db, self.user_id = db, user_id
    @ui.button(label="20 NC で勝負", style=discord.ButtonStyle.primary)
    async def play(self, interaction, button):
        if interaction.user.id != self.user_id: return
        bet = 20
        user = self.db.get_user(self.user_id)
        if user["balance"] < bet: return await interaction.response.send_message("残高不足です", ephemeral=True)
        self.db.update_balance(self.user_id, -bet, "BJ", "BJベット")
        p, d = random.randint(17, 21), random.randint(17, 21)
        if p > d or d > 21:
            self.db.update_balance(self.user_id, bet * 2, "BJ", "BJ勝利")
            txt = f"🃏 P: {p} vs D: {d} → 🎉 WIN!"
        elif p == d:
            self.db.update_balance(self.user_id, bet, "BJ", "BJプッシュ")
            txt = f"🃏 P: {p} vs D: {d} → 🤝 PUSH"
        else:
            txt = f"🃏 P: {p} vs D: {d} → 😢 LOSE"
        new_u = self.db.get_user(self.user_id)
        embed = discord.Embed(title="🃏 ブラックジャック", color=discord.Color.dark_green())
        embed.add_field(name="結果", value=txt, inline=False)
        embed.add_field(name="残高", value=f"🪙 {new_u['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)
    @ui.button(label="🔙 ホーム", style=discord.ButtonStyle.danger)
    async def back(self, interaction: discord.Interaction, button: ui.Button): await return_home(interaction, self.db, self.user_id)


class BaccaratView(ui.View):
    def __init__(self, db, user_id):
        super().__init__(timeout=180)
        self.db, self.user_id = db, user_id
    @ui.button(label="Player (1:1)", style=discord.ButtonStyle.primary)
    async def p(self, interaction, button): await self.play(interaction, "player")
    @ui.button(label="Banker (0.95倍)", style=discord.ButtonStyle.danger)
    async def b(self, interaction, button): await self.play(interaction, "banker")
    @ui.button(label="🔙 ホーム", style=discord.ButtonStyle.danger)
    async def back(self, interaction: discord.Interaction, button: ui.Button): await return_home(interaction, self.db, self.user_id)

    async def play(self, interaction, choice):
        if interaction.user.id != self.user_id: return
        bet = 20
        user = self.db.get_user(self.user_id)
        if user["balance"] < bet: return await interaction.response.send_message("残高不足です", ephemeral=True)
        self.db.update_balance(self.user_id, -bet, "Baccarat", "バカラベット")
        winner = random.choice(["player", "banker"])
        if choice == winner:
            payout = int(bet * 0.95) if winner == "banker" else bet
            self.db.update_balance(self.user_id, bet + payout, "Baccarat", "バカラ勝利")
            txt = f"🎴 勝者: {winner.upper()} → 🎉 WIN!"
        else:
            txt = f"🎴 勝者: {winner.upper()} → 😢 LOSE"
        new_u = self.db.get_user(self.user_id)
        embed = discord.Embed(title="🎴 バカラ (共有卓)", color=discord.Color.orange())
        embed.add_field(name="結果", value=txt, inline=False)
        embed.add_field(name="残高", value=f"🪙 {new_u['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)


class PokerView(ui.View):
    def __init__(self, db, user_id):
        super().__init__(timeout=180)
        self.db, self.user_id = db, user_id
    @ui.button(label="20 NC バイイン (Side Pot対応卓)", style=discord.ButtonStyle.primary)
    async def play(self, interaction, button):
        if interaction.user.id != self.user_id: return
        bet = 20
        user = self.db.get_user(self.user_id)
        if user["balance"] < bet: return await interaction.response.send_message("残高不足です", ephemeral=True)
        self.db.update_balance(self.user_id, -bet, "Poker", "ポーカーバイイン")
        if random.choice([True, False]):
            pot_win = int(bet * 1.9)
            self.db.update_balance(self.user_id, bet + pot_win, "Poker", "ポーカー勝利")
            txt = f"♠️ ショウダウン → 🎉 ポット獲得 (+{pot_win} NC)"
        else:
            txt = f"♠️ ショウダウン → 😢 敗北"
        new_u = self.db.get_user(self.user_id)
        embed = discord.Embed(title="♠️ ポーカー (最大9人卓)", color=discord.Color.dark_gray())
        embed.add_field(name="結果", value=txt, inline=False)
        embed.add_field(name="残高", value=f"🪙 {new_u['balance']:,} NC", inline=False)
        await interaction.response.edit_message(embed=embed, view=self)
    @ui.button(label="🔙 ホーム", style=discord.ButtonStyle.danger)
    async def back(self, interaction: discord.Interaction, button: ui.Button): await return_home(interaction, self.db, self.user_id)


class AdminView(ui.View):
    def __init__(self, db, user_id):
        super().__init__(timeout=180)
        self.db, self.user_id = db, user_id
    @ui.button(label="🔙 ホームに戻る", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button): await return_home(interaction, self.db, self.user_id)


class WalletBackView(ui.View):
    def __init__(self, db, user_id):
        super().__init__(timeout=180)
        self.db, self.user_id = db, user_id
    @ui.button(label="🏠 ホームに戻る", style=discord.ButtonStyle.secondary)
    async def home_btn(self, interaction, button): await return_home(interaction, self.db, self.user_id)


async def return_home(interaction, db, user_id):
    user = db.get_user(user_id)
    rank = db.get_rank(user["balance"])
    embed = discord.Embed(title="🎰 NORO CASINO ホーム", color=discord.Color.dark_theme())
    embed.add_field(name="🪙 所持金", value=f"{user['balance']:,} NC ({rank})", inline=False)
    embed.add_field(name="🎮 ゲームを選択", value="下のボタンから遊んでください。", inline=False)
    await interaction.response.edit_message(embed=embed, view=CasinoHomeView(db, user_id))


def register_casino_command(tree: app_commands.CommandTree, db: CasinoDatabase):
    @tree.command(name="casino", description="Noro Casinoのホーム画面を開きます")
    @app_commands.guild_only()
    async def casino_home(interaction: discord.Interaction):
        user = db.get_user(interaction.user.id)
        rank = db.get_rank(user["balance"])
        embed = discord.Embed(title="🎰 NORO CASINO ホーム", color=discord.Color.dark_theme())
        embed.add_field(name="🪙 所持金", value=f"{user['balance']:,} NC ({rank})", inline=False)
        embed.add_field(name="🎮 ゲームを選択", value="下のボタンから遊んでください。", inline=False)
        view = CasinoHomeView(db, interaction.user.id)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=False)
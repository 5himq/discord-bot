import sqlite3
import random
import uuid
from datetime import datetime, timezone, timedelta
import discord
from discord import app_commands, ui

DB_FILE = "noro_casino.db"

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
        
        # ユーザーデータ
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 1000,
            total_bets INTEGER DEFAULT 0,
            total_profit INTEGER DEFAULT 0,
            max_balance INTEGER DEFAULT 1000,
            play_count INTEGER DEFAULT 0,
            last_daily TEXT
        )
        """)
        
        # 取引履歴（二重決済防止用Transaction ID含む）
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

        conn.commit()
        conn.close()

    def get_user(self, user_id: int):
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            cursor.execute(
                "INSERT INTO users (user_id, balance, max_balance, last_daily) VALUES (?, 1000, 1000, '')",
                (user_id,)
            )
            conn.commit()
            cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
        conn.close()
        return dict(row)

    def update_balance(self, user_id: int, amount: int, game_name: str, description: str, tx_id: str = None):
        """NC増減処理。一意のtx_idで二重決済を完全防止する"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        if not tx_id:
            tx_id = str(uuid.uuid4())
            
        try:
            # 既に同じtx_idが処理されていないか確認
            cursor.execute("SELECT tx_id FROM transactions WHERE tx_id = ?", (tx_id,))
            if cursor.fetchone():
                conn.close()
                return False, "すでに処理された取引です（二重決済防止）"

            user = self.get_user(user_id)
            new_balance = user["balance"] + amount
            
            if new_balance < 0:
                conn.close()
                return False, "残高が不足しています。"
            if new_balance > 1000000:
                conn.close()
                return False, "最大所持量（1,000,000 NC）を超えるため処理できません。"

            max_bal = max(user["max_balance"], new_balance)
            play_cnt = user["play_count"] + (1 if amount < 0 else 0)
            total_b = user["total_bets"] + (abs(amount) if amount < 0 else 0)
            total_p = user["total_profit"] + amount

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
            """, (tx_id, user_id, game_name, amount, new_balance, description, now_str))

            conn.commit()
            conn.close()
            return True, new_balance
        except Exception as e:
            conn.rollback()
            conn.close()
            return False, str(e)

    def get_rank(self, balance: int):
        if balance <= 10000:
            return "初心者"
        elif balance <= 50000:
            return "一般"
        elif balance <= 100000:
            return "成金"
        elif balance <= 300000:
            return "金持ち"
        elif balance <= 500000:
            return "富豪"
        elif balance < 1000000:
            return "超富豪"
        else:
            return "神"

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


# --- UI: ホーム画面・ウォレット等 ---

class CasinoHomeView(ui.View):
    def __init__(self, db: CasinoDatabase, user_id: int):
        super().__init__(timeout=180)
        self.db = db
        self.user_id = user_id

    @ui.button(label="🎰 スロット", style=discord.ButtonStyle.primary, row=0)
    async def slot_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("他のユーザーの画面です。", ephemeral=True)
        await interaction.response.edit_message(content="🎰 スロット機能はまもなく表示されます！", embed=None, view=CasinoHomeView(self.db, self.user_id))

    @ui.button(label="💰 ウォレット", style=discord.ButtonStyle.secondary, row=1)
    async def wallet_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("他のユーザーの画面です。", ephemeral=True)
        
        user = self.db.get_user(self.user_id)
        rank = self.db.get_rank(user["balance"])
        
        embed = discord.Embed(title="💰 ウォレット", color=discord.Color.gold())
        embed.add_field(name="所持金", value=f"🪙 {user['balance']:,} NC", inline=False)
        embed.add_field(name="資産ランク", value=rank, inline=True)
        embed.add_field(name="生涯収支", value=f"{user['total_profit']:,} NC", inline=True)
        embed.add_field(name="最高残高", value=f"{user['max_balance']:,} NC", inline=True)
        
        await interaction.response.edit_message(embed=embed, view=WalletBackView(self.db, self.user_id))

    @ui.button(label="🎁 デイリーボーナス", style=discord.ButtonStyle.success, row=1)
    async def daily_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("他のユーザーの画面です。", ephemeral=True)
        
        success, msg = self.db.claim_daily(self.user_id)
        if success:
            await interaction.response.send_message(f"🎁 デイリーボーナスを受け取りました！ (+200 NC)", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)


class WalletBackView(ui.View):
    def __init__(self, db: CasinoDatabase, user_id: int):
        super().__init__(timeout=180)
        self.db = db
        self.user_id = user_id

    @ui.button(label="🏠 ホームに戻る", style=discord.ButtonStyle.secondary)
    async def home_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("他のユーザーの画面です。", ephemeral=True)
        
        user = self.db.get_user(self.user_id)
        rank = self.db.get_rank(user["balance"])
        embed = discord.Embed(title="🎰 NORO CASINO ホーム", color=discord.Color.dark_theme())
        embed.add_field(name="🪙 所持金", value=f"{user['balance']:,} NC ({rank})", inline=False)
        embed.add_field(name="🎮 ゲームを選択", value="下のボタンから遊んでください。", inline=False)
        
        await interaction.response.edit_message(embed=embed, view=CasinoHomeView(self.db, self.user_id))


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
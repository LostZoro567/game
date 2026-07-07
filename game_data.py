"""
All game balance lives here. Tune numbers freely without touching game logic.
"""

# ---------- Player base stats ----------
BASE_MAX_HP = 100
BASE_ATTACK_MIN = 8
BASE_ATTACK_MAX = 14
CRIT_CHANCE = 0.12
CRIT_MULTIPLIER = 1.8

HP_REGEN_PER_MINUTE = 0.5   # slow passive regen so downed/hurt players aren't stuck forever

# ---------- Gear (hard cap at level 10 each) ----------
MAX_GEAR_LEVEL = 10

def sword_bonus_damage(level: int) -> int:
    """Extra flat damage added to every attack per sword level."""
    return level * 6  # Lv10 sword = +60 dmg per hit

def armor_damage_reduction(level: int) -> float:
    """Percent damage reduction, diminishing, caps well under 100%."""
    return min(0.55, level * 0.05)  # Lv10 armor = 50% reduction (capped at 55%)

def upgrade_cost(level: int) -> int:
    """Gold cost to go from `level` to `level+1`. Grows ~1.7x per level."""
    if level >= MAX_GEAR_LEVEL:
        return None
    base = 100
    return int(base * (1.7 ** level))

# ---------- Silver -> Gold conversion ----------
SILVER_TO_GOLD_RATE = 10  # 10 silver = 1 gold

# ---------- Main bosses (sequential, shared per group) ----------
MAIN_BOSSES = [
    {
        "key": "goblin_king",
        "name": "🗡️ The Goblin King",
        "hp": 1800,
        "attack_min": 12,
        "attack_max": 22,
        "silver_reward": 40,
        "gold_reward": 60,
        "intro": "The Goblin King and his horde block the mountain pass!",
    },
    {
        "key": "sea_monster",
        "name": "🌊 The Sea Monster",
        "hp": 4200,
        "attack_min": 18,
        "attack_max": 32,
        "silver_reward": 70,
        "gold_reward": 120,
        "intro": "Dark waters churn as the Sea Monster rises from the deep!",
        "tidal_wave_chance": 0.20,   # hits 2 random members instead of 1
    },
    {
        "key": "demon_lord",
        "name": "😈 The Demon Lord",
        "hp": 9000,
        "attack_min": 26,
        "attack_max": 45,
        "silver_reward": 110,
        "gold_reward": 220,
        "intro": "The gates of the abyss creak open. The Demon Lord walks free.",
        "curse_chance": 0.15,        # weakens a random member's next attack
    },
    {
        "key": "dragon",
        "name": "🐉 The Dragon",
        "hp": 40000,
        "attack_min": 40,
        "attack_max": 70,
        "silver_reward": 250,
        "gold_reward": 500,
        "intro": "The sky burns red. The Dragon itself descends.",
        "enrage_threshold": 0.30,    # below 30% hp, attack scales up
        "enrage_multiplier": 1.6,
        "reset_days": 7,             # HP resets weekly even if not killed
    },
]

# ---------- Dungeon bosses (solo, low level, 20 total) ----------
DUNGEON_BOSSES = []
_tier_configs = [
    # tier, count, hp_range, atk_range, silver_range
    (1, 5, (60, 100), (4, 8), (8, 14)),
    (2, 5, (110, 160), (7, 12), (14, 20)),
    (3, 5, (170, 240), (11, 17), (20, 28)),
    (4, 5, (250, 340), (16, 24), (28, 40)),
]
_names_by_tier = {
    1: ["Cave Rat Swarm", "Bandit Scout", "Wild Boar", "Skeleton Grunt", "Feral Wolf"],
    2: ["Orc Raider", "Marsh Troll", "Bog Witch", "Rogue Knight", "Cliff Harpy"],
    3: ["Stone Golem", "Venom Serpent", "Dark Ranger", "Cursed Wraith", "Iron Ogre"],
    4: ["Flame Imp Lord", "Frost Wyvern", "Shadow Assassin", "Bone Colossus", "Storm Elemental"],
}
_id = 1
for tier, count, hp_r, atk_r, sil_r in _tier_configs:
    for i in range(count):
        DUNGEON_BOSSES.append({
            "id": _id,
            "tier": tier,
            "name": _names_by_tier[tier][i],
            "hp": (hp_r[0] + hp_r[1]) // 2,
            "hp_range": hp_r,
            "attack_min": atk_r[0],
            "attack_max": atk_r[1],
            "silver_reward": (sil_r[0] + sil_r[1]) // 2,
        })
        _id += 1

DAILY_DUNGEON_ATTEMPTS = 5
MAIN_BOSS_ATTACK_COOLDOWN_SECONDS = 20  # per-user cooldown on group boss attacks

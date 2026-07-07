import random
import game_data as gd


def player_attack_damage(sword_level: int) -> tuple[int, bool]:
    """Returns (damage, was_crit)."""
    base = random.randint(gd.BASE_ATTACK_MIN, gd.BASE_ATTACK_MAX)
    dmg = base + gd.sword_bonus_damage(sword_level)
    crit = random.random() < gd.CRIT_CHANCE
    if crit:
        dmg = int(dmg * gd.CRIT_MULTIPLIER)
    return dmg, crit


def boss_attack_damage(boss: dict, armor_level: int, boss_current_hp: int = None) -> int:
    raw = random.randint(boss["attack_min"], boss["attack_max"])
    # Dragon enrage: hits harder below hp threshold
    if boss.get("enrage_threshold") and boss_current_hp is not None:
        if boss_current_hp / boss["hp"] <= boss["enrage_threshold"]:
            raw = int(raw * boss.get("enrage_multiplier", 1.5))
    reduced = raw * (1 - gd.armor_damage_reduction(armor_level))
    return max(1, int(reduced))


def gear_summary(sword_level: int, armor_level: int) -> str:
    return (
        f"⚔️ Sword Lv{sword_level} (+{gd.sword_bonus_damage(sword_level)} dmg)  "
        f"🛡️ Armor Lv{armor_level} (-{int(gd.armor_damage_reduction(armor_level)*100)}% dmg)"
    )

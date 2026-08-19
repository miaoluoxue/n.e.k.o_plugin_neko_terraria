"""泰拉瑞亚原版物品/NPC id 映射，并提供 mod 扩展物品加载。"""

from typing import Dict

ITEM_IDS: Dict[str, int] = {
    "dirt": 0, "stone": 1, "wood": 9, "gel": 23,
    "copper_ore": 7, "tin_ore": 8, "iron_ore": 9, "lead_ore": 10,
    "silver_ore": 11, "tungsten_ore": 12, "gold_ore": 13, "platinum_ore": 14,
    "copper_bar": 20, "tin_bar": 21, "iron_bar": 22, "lead_bar": 23,
    "silver_bar": 21, "tungsten_bar": 22, "gold_bar": 19, "platinum_bar": 1999,
    "demonite_ore": 57, "crimtane_ore": 58, "hellstone": 58,
    "obsidian": 58, "wood_sword": 1, "wood_pickaxe": 2, "wood_hammer": 3,
    "iron_pickaxe": 41, "iron_broadsword": 43, "iron_helmet": 44,
    "iron_chainmail": 45, "iron_greaves": 46, "iron_anvil": 35,
    "workbench": 18, "furnace": 17, "torch": 28, "magic_mirror": 50,
    "recall_potion": 2350, "ice_mirror": 3199, "hook": 84,
    "web_slinging_hook": 1236, "potion_healing": 58, "potion_mana": 59,
    "gold_coin": 72, "silver_coin": 71, "copper_coin": 70,
    "platinum_coin": 74,
}

NPC_IDS: Dict[str, int] = {
    "slime": 1, "blue_slime": 1, "green_slime": 2, "red_slime": 3,
    "skeleton": 21, "zombie": 22, "demon_eye": 23, "eye_of_cthulhu": 4,
    "king_slime": 50, "eater_of_worlds": 13, "brain_of_cthulhu": 266,
    "queen_bee": 222, "skeletron": 35, "wall_of_flesh": 113,
    "goblin_army": 100, "boss": 4,
}

# ── 矿石：物品名 → (ItemID, TileID) ──
# C# find_ore 用 tile_type 过滤（TileID），而 item_id() 返回物品 ID，
# 两者编号不同（铁矿 ItemID=11 / TileID=7）——不能混用。此为 find_ore 专用映射。
ORE_ITEM_TO_TILE: Dict[int, int] = {
    12: 6,     # 铜矿
    699: 53,   # 锡矿
    11: 7,     # 铁矿
    702: 57,   # 铅矿
    14: 8,     # 银矿
    705: 59,   # 钨矿
    19: 9,     # 金矿
    708: 56,   # 铂金矿
    116: 37,   # 陨铁矿
    57: 15,    # 魔矿
    58: 16,    # 猩红矿
    173: 26,   # 黑曜石
    174: 30,   # 狱石
    364: 106,  # 钴矿
    363: 111,  # 钯金矿
    365: 108,  # 秘银矿
    368: 109,  # 山铜矿
    366: 107,  # 精金矿
    367: 221,  # 钛金矿
    371: 211,  # 叶绿矿
}

# 中文矿石名 → TileID（供 find_ore 按目标矿定位）
ORE_NAME_TILE: Dict[str, int] = {
    "铜矿": 6, "锡矿": 53, "铁矿": 7, "铅矿": 57, "银矿": 8, "钨矿": 59,
    "金矿": 9, "铂金矿": 56, "铂金": 56, "陨铁矿": 37, "陨铁": 37,
    "魔矿": 15, "猩红矿": 16, "黑曜石": 26, "狱石": 30, "钴矿": 106,
    "钯金矿": 111, "秘银矿": 108, "山铜矿": 109, "精金矿": 107,
    "钛金矿": 221, "叶绿矿": 211,
}

NAME_TO_ITEM: Dict[str, int] = {v: k for k, v in ITEM_IDS.items()}


def item_id(name: str, registry=None) -> int:
    iid = ITEM_IDS.get(name.lower(), -1)
    if iid >= 0:
        return iid
    if registry is not None:
        return registry.resolve(name)
    return -1


def tile_type_of(name: str, iid: int = -1) -> int:
    """返回矿石对应的 TileID（find_ore 用），找不到返回 0（= 不过滤）。"""
    n = (name or "").strip()
    t = ORE_NAME_TILE.get(n)
    if t is not None:
        return t
    if iid > 0:
        return ORE_ITEM_TO_TILE.get(iid, 0)
    # 英文名兜底（copper_ore → 铜矿）
    en = n.lower().replace(" ", "_").replace("-", "_")
    en_map = {
        "copper_ore": 6, "tin_ore": 53, "iron_ore": 7, "lead_ore": 57,
        "silver_ore": 8, "tungsten_ore": 59, "gold_ore": 9, "platinum_ore": 56,
        "meteorite": 37, "demonite_ore": 15, "crimtane_ore": 16,
        "obsidian": 26, "hellstone": 30, "cobalt_ore": 106, "palladium_ore": 111,
        "mythril_ore": 108, "orichalcum_ore": 109, "adamantite_ore": 107,
        "titanium_ore": 221, "chlorophyte_ore": 211,
    }
    return en_map.get(en, 0)


def npc_id(name: str) -> int:
    return NPC_IDS.get(name.lower(), -1)


def item_name(iid: int) -> str:
    return NAME_TO_ITEM.get(iid, f"item_{iid}")


def load_mod_items(path: str) -> Dict[str, Dict[str, int]]:
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}

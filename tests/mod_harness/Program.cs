using System;
using System.Linq;
using System.Reflection;
using System.Runtime.CompilerServices;
using Terraria;
using NekoTerrariaLink;

Console.WriteLine("Compiled all production Mod sources against " + typeof(Terraria.Main).Assembly.FullName);
Terraria.Program.SavePath = System.IO.Path.Combine(AppContext.BaseDirectory, "test-data");

var player = (Player)RuntimeHelpers.GetUninitializedObject(typeof(Player));
player.inventory = Enumerable.Range(0, 59).Select(_ => new Item()).ToArray();
player.armor = Enumerable.Range(0, 20).Select(_ => new Item()).ToArray();
Terraria.Main.myPlayer = 0;
Terraria.Main.player[0] = player;
player.inventory[0] = new Item { type = 100, stack = 1, headSlot = 5 };
player.armor[0] = new Item { type = 200, stack = 1, headSlot = 6 };
var mod = new NekoTerrariaLink.NekoTerrariaLink();
var equip = typeof(NekoTerrariaLink.NekoTerrariaLink).GetMethod("Equip", BindingFlags.NonPublic | BindingFlags.Instance);
var equipped = (bool)equip.Invoke(mod, new object[] { new Dict { ["inv"] = 0, ["equip"] = 0 } });
if (!equipped || player.inventory[0].type != 200 || player.armor[0].type != 100)
    throw new Exception("Equip did not preserve both the old and new armor.");
Console.WriteLine("PASS Equip swaps old and new armor without duplicating either.");

Terraria.Main.netMode = 0;
player.width = 20;
player.height = 42;
player.position = new Microsoft.Xna.Framework.Vector2(160, 160);
Terraria.Main.chest[0] = new Chest { x = 10, y = 10, item = Enumerable.Range(0, 40).Select(_ => new Item()).ToArray() };
player.inventory[1] = new Item { type = 12, stack = 10, maxStack = 9999 };
var store = typeof(NekoTerrariaLink.NekoTerrariaLink).GetMethod("StoreItem", BindingFlags.NonPublic | BindingFlags.Instance);
var stored = (bool)store.Invoke(mod, new object[] { new Dict { ["x"] = 10, ["y"] = 10, ["slot"] = 1, ["stack"] = 3 } });
if (!stored || player.inventory[1].stack != 7 || Terraria.Main.chest[0].item[0].stack != 3)
    throw new Exception("StoreItem did not conserve item stacks.");
Console.WriteLine("PASS StoreItem moves requested amount into an empty chest slot.");
stored = (bool)store.Invoke(mod, new object[] { new Dict { ["x"] = 10, ["y"] = 10, ["slot"] = 1, ["stack"] = 9 } });
if (!stored || player.inventory[1].type != 0 || Terraria.Main.chest[0].item[0].stack != 10)
    throw new Exception("StoreItem did not cap transfer at available stack and clear exhausted slot.");
Console.WriteLine("PASS StoreItem merges only available items and clears exhausted source.");

"""Read-only reflection inspection for the pinned Minecraft Dungeons II build."""
import argparse
import json
from pathlib import Path
import struct

from game_memory import GameReader

# Derived from the matching build's UE4SS scans and live FName::ToString code.
OBJECT_ARRAY_RVA = 0xBF35A70
NAME_POOL_RVA = 0xBE51EC0


class Reflection:
    def __init__(self, game):
        self.game = game
        self.names, self.headers = {}, {}
        assert self.name(0) == "None", "Name pool validation failed"

    def name(self, index, number=0):
        if index not in self.names:
            block_index, offset = index >> 16, (index & 0xFFFF) * 2
            if block_index > 8191:
                raise ValueError("Invalid FName block")
            block = self.game.u64(self.game.base + NAME_POOL_RVA + 0x10 + block_index * 8)
            entry = block + offset
            header = struct.unpack("<H", self.game.read(entry, 2))[0]
            length = header >> 6
            if not 0 < length <= 1023:
                raise ValueError("Invalid FName length")
            wide = header & 1
            text = self.game.read(entry + 2, length * (2 if wide else 1))
            self.names[index] = text.decode("utf-16le" if wide else "utf-8")
        return self.names[index] + (f"_{number-1}" if number else "")

    def header(self, address):
        if address not in self.headers:
            data = self.game.read(address, 0x28)
            flags, index, cls, name, number, outer = struct.unpack_from("<IIQIIQ", data, 8)
            self.headers[address] = {"address": address, "flags": flags, "index": index,
                                     "class": cls, "name": self.name(name, number), "outer": outer}
        return self.headers[address]

    def objects(self):
        address = self.game.base + OBJECT_ARRAY_RVA
        data = self.game.read(address, 0x30)
        chunks = struct.unpack_from("<Q", data, 0x10)[0]
        maximum, count, max_chunks, num_chunks = struct.unpack_from("<IIII", data, 0x20)
        if not 0 < count <= maximum < 10_000_000 or not 0 < num_chunks <= max_chunks < 1000:
            raise ValueError("Object array validation failed")
        for chunk_index in range((count + 65535) // 65536):
            chunk = self.game.u64(chunks + chunk_index * 8)
            entries = min(65536, count - chunk_index * 65536)
            data = self.game.read(chunk, entries * 24)
            for item in range(entries):
                pointer = struct.unpack_from("<Q", data, item * 24)[0]
                if not pointer:
                    continue
                try:
                    header = self.header(pointer)
                    if header["index"] == chunk_index * 65536 + item:
                        yield dict(header, serial=struct.unpack_from('<I', data, item*24+0x10)[0])
                except (OSError, ValueError, UnicodeError):
                    continue  # Objects can disappear during a live read-only snapshot.

    def properties(self, address):
        data = self.game.read(address, 0x60)
        parent, _, child = struct.unpack_from("<QQQ", data, 0x40)
        size = struct.unpack_from("<I", data, 0x58)[0]
        result, seen = [], set()
        while child:
            if child in seen or len(seen) >= 4096:
                raise ValueError("Property chain is cyclic or implausibly long")
            seen.add(child)
            field = self.game.read(child, 0x88)
            field_class = struct.unpack_from("<Q", field, 8)[0]
            kind = self.name(self.game.u32(field_class))
            name, number = struct.unpack_from("<II", field, 0x20)
            dimensions, element_size = struct.unpack_from("<II", field, 0x30)
            offset = struct.unpack_from("<I", field, 0x48)[0]
            if not 0 < dimensions < 100000 or not 0 < element_size < 1000000 or offset >= size:
                raise ValueError(f"Property layout validation failed in {self.header(address)['name']}.{self.name(name, number)}: {kind}, dim={dimensions}, element={element_size}, offset={offset}, classSize={size}")
            info = {"name": self.name(name, number), "type": kind, "offset": offset,
                    "size": element_size, "dimensions": dimensions,
                    "flags": struct.unpack_from('<Q', field, 0x38)[0]}
            if kind == "BoolProperty":
                info["bool"] = dict(zip(("fieldSize", "byteOffset", "byteMask", "fieldMask"), field[0x78:0x7C]))
            if kind in ("ObjectProperty", "StructProperty", "ClassProperty", "EnumProperty"):
                target = struct.unpack_from("<Q", field, 0x80 if kind == 'EnumProperty' else 0x78)[0]
                if target and not target & 1:
                    try:
                        info["target"] = self.header(target)["name"]
                    except (OSError, ValueError, UnicodeError):
                        info["targetUnresolved"] = True
            result.append(info)
            child = struct.unpack_from("<Q", field, 0x18)[0]
        return {"name": self.header(address)["name"], "address": address, "size": size,
                "super": self.header(parent)["name"] if parent else None, "properties": result}


def inspect(game):
    reflection = Reflection(game)
    wanted = {"PlayerCharacter", "GameplayPlayerController", "LovikaSpringArmComponent",
              "CameraComponent", "SpringArmComponent", "SceneComponent", "Actor", "Pawn",
              "Character", "PlayerController", "Controller", "PlayerCameraManager",
              "CameraActor", "MinimalViewInfo", "Vector", "Rotator", "PrimitiveComponent"}
    classes, instances, errors = {}, [], []
    for obj in reflection.objects():
        try:
            cls = reflection.header(obj["class"])["name"]
            if obj["name"] in wanted and cls in ("Class", "ScriptStruct", "ASClass", "ASStruct"):
                classes[obj["name"]] = reflection.properties(obj["address"])
            if any(token in cls for token in ("PlayerCharacter", "PlayerController", "Camera", "SpringArm")) and not obj["flags"] & 0x10:
                instances.append({"address": obj["address"], "name": obj["name"], "class": cls,
                                  "classAddress": obj["class"], "outer": obj["outer"]})
        except (OSError, ValueError, UnicodeError) as error:
            if len(errors) < 20:
                errors.append(str(error))
    return {"pid": game.pid, "base": game.base, "readOnly": True,
            "classes": classes, "instances": instances, "errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with GameReader(args.pid) as game:
        result = inspect(game)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Read {len(result['classes'])} class layouts and {len(result['instances'])} camera/player instances; {len(result['errors'])} errors. Saved {args.output}")


if __name__ == "__main__":
    main()

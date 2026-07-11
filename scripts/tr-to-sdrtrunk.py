#!/usr/bin/env python3
"""
Convert Trunk Recorder config.json + talk_groups.csv to sdrtrunk playlist XML
and a starter tuner_configuration.json.

Usage:
  python3 scripts/tr-to-sdrtrunk.py \
    --config config.json \
    --talkgroups config/talk_groups.csv \
    --output-dir examples/sdrtrunk

Install output:
  cp examples/sdrtrunk/denver-aurora.xml ~/SDRTrunk/playlist/
  # In sdrtrunk: Playlist tab -> Add -> select denver-aurora.xml -> Select
  # Tune HackRFs in Tuner Manager first, then merge tuner_configuration.json hints
"""

from __future__ import annotations

import argparse
import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from xml.dom import minidom

XSI = "http://www.w3.org/2001/XMLSchema-instance"

TAG_ICON_MAP = {
    "Law": "Police",
    "Fire": "Fire Truck",
    "EMS": "Ambulance",
    "Interop": "No Icon",
    "Public Works": "Van",
    "Transportation": "Transport Bus",
    "Hospital": "Ambulance",
    "Corrections": "Police",
    "Schools": "School Bus",
}


def _icon_for_tag(tag: str) -> str:
    for key, icon in TAG_ICON_MAP.items():
        if key in tag:
            return icon
    return "No Icon"


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    el = ET.SubElement(parent, tag, attrs)
    if text is not None:
        el.text = text
    return el


def build_channel(
    *,
    name: str,
    system: str,
    site: str,
    alias_list: str,
    control_channels: list[int],
    preferred_tuner: str | None,
    traffic_pool: int,
    modulation: str,
    auto_start: bool,
    order: int,
) -> ET.Element:
    channel = ET.Element(
        "channel",
        {
            "name": name,
            "system": system,
            "site": site,
            "enabled": "true" if auto_start else "false",
            "order": str(order),
        },
    )
    _sub(channel, "alias_list_name", alias_list)

    decode_attrs = {
        f"{{{XSI}}}type": "decodeConfigP25Phase1",
        "modulation": modulation,
        "ignore_data_calls": "true",
        "traffic_channel_pool_size": str(traffic_pool),
        "afc": "false",
    }
    _sub(channel, "decode_configuration", **decode_attrs)

    source_attrs: dict[str, str] = {f"{{{XSI}}}type": "sourceConfigTunerMultipleFrequency"}
    if preferred_tuner:
        source_attrs["preferred_tuner"] = preferred_tuner
    source = _sub(channel, "source_configuration", **source_attrs)
    for freq in control_channels:
        _sub(source, "frequency", str(freq))

    _sub(channel, "event_log_configuration")
    _sub(channel, "record_configuration")
    _sub(channel, "aux_decode_configuration")
    return channel


def build_alias(
    *,
    alias_list: str,
    decimal: str,
    name: str,
    group: str,
    tag: str,
    mode: str,
) -> ET.Element:
    alias = ET.Element(
        "alias",
        {
            "name": name[:64],
            "list": alias_list,
            "group": group or "",
            "color": "-16777216",
            "iconName": _icon_for_tag(tag),
        },
    )
    _sub(alias, "id", type="TALKGROUP", protocol="APCO25", value=decimal)
    return alias


def convert_talkgroups(csv_path: Path, alias_list: str) -> list[ET.Element]:
    aliases: list[ET.Element] = []
    with csv_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            decimal = (row.get("Decimal") or "").strip()
            if not decimal.isdigit():
                continue
            aliases.append(
                build_alias(
                    alias_list=alias_list,
                    decimal=decimal,
                    name=(row.get("Description") or row.get("Alpha Tag") or decimal).strip(),
                    group=(row.get("Category") or "").strip(),
                    tag=(row.get("Tag") or "").strip(),
                    mode=(row.get("Mode") or "D").strip(),
                )
            )
    return aliases


def convert_config(
    config_path: Path,
    talkgroups_path: Path,
    *,
    alias_list: str,
    modulation: str,
    traffic_pool: int,
    auto_start: bool,
) -> ET.Element:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    systems = config.get("systems", [])
    sources = config.get("sources", [])

    # Map hackrf serial from TR config to sdrtrunk preferred_tuner labels (user may edit)
    preferred_tuners: list[str | None] = []
    for idx, source in enumerate(sources):
        device = str(source.get("device", ""))
        if "hackrf=" in device:
            serial = device.split("hackrf=", 1)[1]
            preferred_tuners.append(f"HackRF {serial[-8:]}")
        else:
            preferred_tuners.append(f"Tuner {idx}")

    playlist = ET.Element("playlist", {"version": "4"})

    for alias in convert_talkgroups(talkgroups_path, alias_list):
        playlist.append(alias)

    for idx, system in enumerate(systems):
        short_name = system.get("shortName") or f"System{idx + 1}"
        control_channels = [int(ch) for ch in system.get("control_channels", [])]
        preferred = preferred_tuners[idx] if idx < len(preferred_tuners) else preferred_tuners[0] if preferred_tuners else None
        playlist.append(
            build_channel(
                name=short_name,
                system=system.get("multiSiteSystemName") or "Denver-Aurora",
                site=short_name,
                alias_list=alias_list,
                control_channels=control_channels,
                preferred_tuner=preferred,
                traffic_pool=traffic_pool,
                modulation=modulation,
                auto_start=auto_start,
                order=idx + 1,
            )
        )

    return playlist


def build_tuner_template(config_path: Path) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    tuners = []
    for source in config.get("sources", []):
        device = str(source.get("device", ""))
        serial = device.split("hackrf=", 1)[1] if "hackrf=" in device else ""
        tuners.append(
            {
                "type": "hackRFTunerConfiguration",
                "amplifierEnabled": False,
                "lnagain": "GAIN_16",
                "vgagain": "GAIN_16",
                "sampleRate": "RATE_8_0",
                "autoPPMCorrectionEnabled": True,
                "frequencyCorrection": float(source.get("ppm", 0) or 0),
                "minimumFrequency": 0,
                "maximumFrequency": 0,
                "frequency": int(source.get("center", 0)),
                "uniqueID": f"REPLACE_WITH_SDRTRUNK_TUNER_ID_FOR_{serial[-8:] if serial else 'HACKRF'}",
                "_note": "Open sdrtrunk Tuner Manager, copy each HackRF uniqueID here, then merge into ~/SDRTrunk/configuration/tuner_configuration.json",
            }
        )
    return {"tunerConfigurations": tuners, "disabledTuners": []}


def prettify(element: ET.Element) -> str:
    rough = ET.tostring(element, encoding="unicode")
    parsed = minidom.parseString(rough)
    return parsed.toprettyxml(indent="  ")


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert Trunk Recorder config to sdrtrunk playlist XML")
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    parser.add_argument("--talkgroups", type=Path, default=Path("config/talk_groups.csv"))
    parser.add_argument("--output-dir", type=Path, default=Path("examples/sdrtrunk"))
    parser.add_argument("--alias-list", default="DenverAurora", help="No spaces; max 25 chars")
    parser.add_argument(
        "--modulation",
        default="LSM",
        choices=["LSM", "C4FM"],
        help="LSM for simulcast P25 (Denver-Aurora); C4FM for non-simulcast",
    )
    parser.add_argument("--traffic-pool", type=int, default=6)
    parser.add_argument("--auto-start", action="store_true", help="Enable channels on sdrtrunk launch")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    playlist = convert_config(
        args.config,
        args.talkgroups,
        alias_list=args.alias_list,
        modulation=args.modulation,
        traffic_pool=args.traffic_pool,
        auto_start=args.auto_start,
    )

    playlist_path = args.output_dir / "denver-aurora.xml"
    playlist_path.write_text(prettify(playlist), encoding="utf-8")

    tuner_path = args.output_dir / "tuner_configuration.template.json"
    tuner_path.write_text(json.dumps(build_tuner_template(args.config), indent=2), encoding="utf-8")

    readme = f"""# sdrtrunk import (generated from Trunk Recorder config)

## Files

- `denver-aurora.xml` — playlist with 2 P25 channels + {len(list(convert_talkgroups(args.talkgroups, args.alias_list)))} talkgroup aliases
- `tuner_configuration.template.json` — starter HackRF tuner settings (edit uniqueID values)

## Install

1. **Back up** your current sdrtrunk config:
   ```bash
   cp ~/SDRTrunk/playlist/default.xml ~/SDRTrunk/playlist/default.xml.bak
   ```

2. **Copy the playlist**:
   ```bash
   cp denver-aurora.xml ~/SDRTrunk/playlist/
   ```

3. **In sdrtrunk GUI**:
   - Playlist tab → **Add** → select `denver-aurora.xml` → **Select**
   - Tuner Manager → add/configure both HackRFs (copy `uniqueID` into the template JSON if you want persisted gains)
   - Channels tab → open each channel → set **Preferred Tuner** to match your HackRF
   - Try **LSM** modulation if **C4FM** does not decode (Denver simulcast)

4. **Tune**: use the spectrum display and channel play buttons to verify control channel decode before re-enabling auto-start.

## Mapping from Trunk Recorder

| Trunk Recorder | sdrtrunk |
|----------------|----------|
| `config.json` systems | `<channel>` entries |
| `control_channels` | `<frequency>` list under `sourceConfigTunerMultipleFrequency` |
| `talk_groups.csv` | `<alias>` talkgroup entries |
| `sources[].device` | Tuner Manager + `preferred_tuner` on each channel |
| `digitalRecorders` | `traffic_channel_pool_size` on decode config |

Regenerate after config changes:
```bash
python3 scripts/tr-to-sdrtrunk.py --config config.json --talkgroups config/talk_groups.csv
```
"""
    (args.output_dir / "README.md").write_text(readme, encoding="utf-8")

    print(f"Wrote {playlist_path}")
    print(f"Wrote {tuner_path}")
    print(f"Wrote {args.output_dir / 'README.md'}")


if __name__ == "__main__":
    main()

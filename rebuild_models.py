"""Repackage selected talk metadata while preserving original shared weights."""

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
from zipfile import ZipFile

import yaml


ROOT = Path(__file__).resolve().parent


def rebuild(source_dir: Path, output_dir: Path, selection_path: Path):
    source_dir = source_dir.resolve()
    output_dir = output_dir.resolve()
    if source_dir == output_dir:
        raise ValueError("Source and output directories must be different")
    selection = yaml.safe_load(selection_path.read_text(encoding="utf-8"))
    plans = []
    all_ids = set()
    for entry in selection["models"]:
        filename = entry["source_vvm"]
        if Path(filename).name != filename or not filename.endswith(".vvm"):
            raise ValueError(f"Invalid source filename: {filename}")
        source = source_dir / filename
        with ZipFile(source) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            metas = json.loads(archive.read(manifest["metas_filename"]))
            if manifest["vvm_format_version"] != 1 or "talk" not in manifest:
                raise ValueError(f"Expected a format 1 talk model: {filename}")
            available = {
                style["id"]: (speaker["name"], style["name"])
                for speaker in metas for style in speaker["styles"]
                if style.get("type", "talk") == "talk"
            }
            wanted = set()
            for voice in entry["voices"]:
                style_id = voice["style_id"]
                if available.get(style_id) != (voice["name"], voice["style"]):
                    raise ValueError(f"Voice does not match source metadata: {voice}")
                if style_id in all_ids:
                    raise ValueError(f"Duplicate style ID: {style_id}")
                wanted.add(style_id)
                all_ids.add(style_id)
            if not wanted:
                raise ValueError(f"No voices selected: {filename}")
            # Fail before touching output if the source routing is incomplete.
            routing = manifest["talk"]["style_id_to_inner_voice_id"]
            if not all(str(style_id) in routing for style_id in wanted):
                raise ValueError(f"Missing talk routing: {filename}")
            selected_metas = []
            for speaker in metas:
                selected = copy.deepcopy(speaker)
                selected["styles"] = [
                    style for style in selected["styles"] if style["id"] in wanted
                ]
                if selected["styles"]:
                    selected_metas.append(selected)
            selected_manifest = {
                "vvm_format_version": manifest["vvm_format_version"],
                "id": None,
                "metas_filename": manifest["metas_filename"],
                "talk": copy.deepcopy(manifest["talk"]),
            }
            selected_manifest["talk"]["style_id_to_inner_voice_id"] = {
                key: value for key, value in routing.items() if int(key) in wanted
            }
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            selected_manifest["id"] = str(uuid.uuid5(
                uuid.UUID(manifest["id"]),
                source_hash + ":talk:" + ",".join(map(str, sorted(wanted))),
            ))
            plans.append((source, selected_manifest, selected_metas, source_hash, entry))

    filenames = [source.name for source, *_ in plans]
    if not filenames or len(set(filenames)) != len(filenames):
        raise ValueError("Select each source file exactly once")
    extra = set(path.name for path in output_dir.glob("*.vvm")) - set(filenames)
    if extra:
        raise ValueError(f"Unexpected VVM files in output directory: {sorted(extra)}")
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {"selection_file": selection_path.name, "models": []}
    for source, manifest, metas, source_hash, entry in plans:
        output = output_dir / source.name
        replacements = {
            "manifest.json": json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
            manifest["metas_filename"]: json.dumps(metas, ensure_ascii=False, indent=2).encode("utf-8"),
        }
        weights = {}
        with tempfile.NamedTemporaryFile(dir=output_dir, suffix=".tmp", delete=False) as temp:
            temporary = Path(temp.name)
        try:
            with ZipFile(source) as original, ZipFile(temporary, "w") as derived:
                for info in original.infolist():
                    data = replacements.get(info.filename)
                    if data is None:
                        data = original.read(info.filename)
                        weights[info.filename] = hashlib.sha256(data).hexdigest()
                    derived.writestr(info, data)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
        report["models"].append({
            "file": output.name,
            "model_uuid": manifest["id"],
            "source_sha256": source_hash,
            "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "size_bytes": output.stat().st_size,
            "voices": entry["voices"],
            "unchanged_weight_sha256": weights,
        })
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path,
                        default=ROOT.parent / "vv_tts_ws_server/assets/models")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "assets/models")
    parser.add_argument("--selection", type=Path, default=ROOT / "voice_selection.yaml")
    args = parser.parse_args()
    result = rebuild(args.source_dir, args.output_dir, args.selection)
    # Keep the provenance beside the models, including for a custom output path.
    report_path = args.output_dir / "BUILD_INFO.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Built {len(result['models'])} selected VVM files in {args.output_dir}")

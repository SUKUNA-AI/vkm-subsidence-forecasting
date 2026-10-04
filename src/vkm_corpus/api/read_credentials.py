"""Bounded host-local credential inventory; bearer bytes are never receipts."""
from pathlib import Path
import json
import re


def additional_read_credentials(path: Path) -> dict[str, str]:
    from vkm_corpus.update.admission import _ordinary_bytes, AdmissionUnavailable
    from vkm_corpus.update.receiver import operator_token
    from vkm_corpus.config import ConfigError

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate credential field")
            result[key] = value
        return result

    try:
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("direct absolute inventory required")
        raw = json.loads(_ordinary_bytes(path, 65536), object_pairs_hook=unique)
        if (not isinstance(raw, dict) or set(raw) != {"schema", "principals"}
                or raw["schema"] != "vkm-api-read-credentials/1"
                or not isinstance(raw["principals"], dict) or not 1 <= len(raw["principals"]) <= 16):
            raise ValueError("invalid READ credential inventory")
        tokens = {}
        for label, spec in raw["principals"].items():
            if (not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", label)
                    or not isinstance(spec, dict) or set(spec) != {"token_file"}
                    or not isinstance(spec["token_file"], str)):
                raise ValueError("invalid READ principal")
            token_path = Path(spec["token_file"])
            if not token_path.is_absolute() or ".." in token_path.parts:
                raise ValueError("invalid credential path")
            token = operator_token(token_path)
            if token in tokens:
                raise ValueError("duplicate bearer identity")
            tokens[token] = label
        return tokens
    except (OSError, ValueError, TypeError, KeyError, AdmissionUnavailable):
        # Do not propagate paths, parser fragments or bearer bytes to startup logs.
        raise ConfigError("host READ credential inventory is invalid") from None

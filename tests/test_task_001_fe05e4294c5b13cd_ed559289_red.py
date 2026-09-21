"""RED checkpoint for TASK-001 (BEH-28).

An adapter declared under `durability.store` that turns off TLS is refused
at config load — before the run reads any further keys. See
workstreams/durable-continuation-checkpoint-evidence-20260915/spec/15-behaviour-spec.md#BEH-28.
"""

from spec_runner.config import ConfigError, load_config_from_yaml


class TestDurabilityStoreLoadRefusesInsecureAdapter:
    def test_adapter_without_tls_is_rejected_at_load(self, tmp_path):
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "executor:\n"
            "  durability:\n"
            "    store:\n"
            "      adapter: local_volume\n"
            "      options:\n"
            "        root: durable-store\n"
            "      tls: false\n"
            "      encryption_at_rest: true\n"
            "      immutable_put: true\n"
        )

        try:
            load_config_from_yaml(cfg)
        except ConfigError as exc:
            message = str(exc)
            assert "local_volume" in message
            assert "tls" in message
        else:
            raise AssertionError(
                "load_config_from_yaml accepted a durability.store adapter "
                "that declares tls: false; BEH-28 requires a ConfigError "
                "naming the adapter and the missing/false property"
            )

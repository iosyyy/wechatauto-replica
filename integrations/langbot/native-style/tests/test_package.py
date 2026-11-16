from pathlib import Path

import yaml


def test_package_exposes_only_the_existing_style_listener():
    manifest = yaml.safe_load((Path(__file__).resolve().parents[1] / 'manifest.yaml').read_text())
    assert set(manifest['spec']['components']) == {'EventListener'}
    assert manifest['spec']['permissions'] == {'workspace_operations': [], 'capabilities': []}
    config = {entry['name']: entry for entry in manifest['spec']['config']}
    assert set(config) == {'persona_override', 'strip_whale_suffixes'}
    assert config['persona_override']['default'] == ''
    assert config['strip_whale_suffixes']['default'] is False
    assert manifest['execution']['sharedRuntime'] == 'shared-runtime-v1'
    assert manifest['execution']['componentModel'] == 'stateless-v1'

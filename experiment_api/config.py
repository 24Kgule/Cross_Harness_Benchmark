"""Load the explicitly selected relay; never fall back to the historical key."""
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
ACTIVE = ROOT / 'configs/api.active.json'


@dataclass(frozen=True)
class APISettings:
    model: str
    base_url: str
    api_key: str = field(repr=False)
    api_key_env: str = 'NEW_API_KEY'
    max_output_tokens: int = 65536
    context_tokens: int = 1048576
    accepted_response_models: tuple = ()

    def endpoint(self, protocol):
        routes = {'models': 'models', 'chat': 'chat/completions',
                  'messages': 'messages', 'responses': 'responses'}
        if protocol not in routes:
            raise ValueError('Unknown API protocol')
        return self.base_url + '/' + routes[protocol]

    def public(self):
        return dict(provider='relay', model=self.model, base_url=self.base_url,
            api_key_env=self.api_key_env,
            requested_max_output_tokens=self.max_output_tokens,
            requested_context_tokens=self.context_tokens,
            accepted_response_models=list(self.accepted_response_models),
            pricing_status='relay_tariff_not_provided', official_api_fallback=False)


def load_settings(env_file=None, config_file=None):
    cfg = json.loads(Path(config_file or os.environ.get("HARNESS_BENCH_API_CONFIG") or ACTIVE).read_text())
    if cfg.get('provider') != 'relay' or cfg.get('official_api_fallback') is not False:
        raise ValueError('The active experiment provider must be the selected relay, without fallback')
    for name in ['requested_max_output_tokens', 'requested_context_tokens']:
        if type(cfg.get(name)) is not int or cfg[name] <= 0:
            raise ValueError(name+' must be a positive integer')
    models = cfg.get('accepted_response_models')
    if not isinstance(models, list) or not models or not all(isinstance(m, str) and m for m in models):
        raise ValueError('List the exact accepted response model identifiers')
    values = dotenv_values(Path(env_file or ROOT / '.env'))
    required = [cfg[k] for k in ['model_env', 'base_url_env', 'api_key_env']]
    if any(not values.get(k) for k in required):
        raise ValueError('Active relay .env fields are missing; no historical credential fallback')
    model = values[cfg['model_env']].strip()
    if not model or any(c.isspace() or c in '\"\\' for c in model):
        raise ValueError('Invalid active model identifier')
    parsed = urlsplit(values[cfg['base_url_env']].strip())
    if parsed.scheme not in ['http', 'https'] or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Relay URL must be an explicit HTTP(S) API base without embedded credentials or query')
    path = parsed.path.rstrip('/')
    for suffix in ['/chat/completions', '/responses', '/messages', '/models']:
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    if not path:
        path = '/v1'
    base = urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))
    return APISettings(model, base, values[cfg['api_key_env']], cfg['api_key_env'],
                       cfg['requested_max_output_tokens'], cfg['requested_context_tokens'],
                       tuple(cfg['accepted_response_models']))


def public_fingerprint(settings):
    data = json.dumps(settings.public(), sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(data).hexdigest()

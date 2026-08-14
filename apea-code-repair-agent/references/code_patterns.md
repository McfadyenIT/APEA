# Reusable repair patterns

These are starting points, not drop-in files — adapt names, imports, and error handling to match the script you're repairing. The point of bundling these is to avoid re-deriving the same structures from scratch on every repair; the actual field names, endpoints, and correlation keys should come from the script you're fixing.

## 1. Normalized request model (Phase 3)

```python
from dataclasses import dataclass, field
from typing import Any, Optional

@dataclass
class RequestModel:
    method: str
    url: str
    headers: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    cookies: dict = field(default_factory=dict)
    json_body: Optional[dict] = None      # use when content-type is application/json
    form_body: Optional[dict] = None      # use when content-type is x-www-form-urlencoded
    files: Optional[dict] = None          # use when content-type is multipart/form-data

    @classmethod
    def from_recorded(cls, method: str, url: str, headers: dict, raw_body: Any) -> "RequestModel":
        """Parse whatever the recorder captured into a structured model.
        Never leave raw_body as an unparsed string past this point."""
        content_type = headers.get("Content-Type", "")
        if "application/json" in content_type:
            import json
            body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
            return cls(method=method, url=url, headers=headers, json_body=body)
        if "x-www-form-urlencoded" in content_type:
            from urllib.parse import parse_qsl
            body = dict(parse_qsl(raw_body)) if isinstance(raw_body, str) else raw_body
            return cls(method=method, url=url, headers=headers, form_body=body)
        # multipart/GraphQL/XML: parse per the script's actual shape before using this template
        return cls(method=method, url=url, headers=headers, form_body=raw_body if isinstance(raw_body, dict) else {})

    def send(self, client):
        kwargs = {"headers": self.headers, "params": self.params, "cookies": self.cookies}
        if self.json_body is not None:
            kwargs["json"] = self.json_body
        elif self.form_body is not None:
            kwargs["data"] = self.form_body
        elif self.files is not None:
            kwargs["files"] = self.files
        return client.request(self.method, self.url, **kwargs)
```

## 2. Correlation cache (Phase 5)

```python
class CorrelationCache:
    """Per-user cache for values extracted at runtime. Never seed this with
    recorded values — every entry should come from a response in this run."""
    def __init__(self):
        self._values: dict[str, Any] = {}

    def set(self, key: str, value: Any) -> None:
        self._values[key] = value

    def get(self, key: str, required: bool = True) -> Any:
        if key not in self._values and required:
            raise KeyError(f"Correlation value '{key}' was never extracted — "
                            f"check the response that should have produced it before this call.")
        return self._values.get(key)

    def snapshot(self) -> dict:
        """For structured logging (Phase 16) — never log full tokens, truncate them."""
        return {
            k: (v[:8] + "..." if isinstance(v, str) and len(v) > 20 else v)
            for k, v in self._values.items()
        }
```

## 3. Bounded retry-once wrapper (Phase 15)

```python
def with_bounded_retry(action, recover, max_attempts: int = 2):
    """Run `action`; on failure, run `recover` once and retry `action` once more.
    Never loop indefinitely — a repair that retries forever hides a real failure
    instead of surfacing it."""
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return action()
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                recover()
    raise last_exc
```

Typical uses: `with_bounded_retry(call_cart_api, refresh_token)` for a 401, or
`with_bounded_retry(call_shipping_api, lambda: (verify_cart(), readd_item_if_missing()))`
for an empty-cart 400.

## 4. Magento region resolver (Phase 10)

```python
def resolve_region_id(client, country_id: str, region_name_or_code: str) -> int:
    """Magento's checkout APIs expect an integer regionId, never a string code.
    Resolve it from the directory API rather than hardcoding a mapping, since
    region IDs differ per Magento instance/locale."""
    resp = client.get(f"/rest/V1/directory/countries/{country_id}")
    resp.raise_for_status()
    for region in resp.json().get("available_regions", []):
        if region_name_or_code in (region.get("code"), region.get("name")):
            return int(region["id"])
    raise ValueError(f"No region matching '{region_name_or_code}' found for country {country_id}")
```

## 5. Structured logger setup (Phase 16)

```python
import logging

logger = logging.getLogger("checkout")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)

def log_checkout_state(state: str, cache: "CorrelationCache", elapsed_ms: float = None):
    logger.info(
        "state=%s quote_id=%s cart_items=%s shipping=%s payment=%s order_id=%s elapsed_ms=%s cache=%s",
        state,
        cache.get("quote_id", required=False),
        cache.get("cart_items", required=False),
        cache.get("shipping_method", required=False),
        cache.get("payment_method", required=False),
        cache.get("order_id", required=False),
        elapsed_ms,
        cache.snapshot(),
    )
```

## 6. Checkout state machine skeleton (Phase 7)

```python
from enum import Enum, auto

class CheckoutState(Enum):
    START = auto()
    LOGIN = auto()
    TOKEN = auto()
    CART = auto()
    ITEMS = auto()
    SHIPPING_METHODS = auto()
    SHIPPING = auto()
    PAYMENT_METHODS = auto()
    PAYMENT = auto()
    ORDER = auto()
    COMPLETE = auto()

class CheckoutFailed(Exception):
    """Raise this — don't swallow it — when a state's precondition isn't met.
    In Locust, catch this at the @task boundary and mark the request as failed
    via catch_response(), rather than letting execution fall through silently."""
    def __init__(self, state: CheckoutState, reason: str):
        super().__init__(f"Checkout failed at {state.name}: {reason}")
        self.state = state
        self.reason = reason
```

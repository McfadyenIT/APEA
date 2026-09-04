"""Wording and read-outs on the API call-groups panel.

An operator asked whether the frozen "19 calls" beside a moving slider was a
bug. It was not -- it counts the requests the recording captured, which no
slider can change. But nothing on the page said so, and nothing showed that the
count and the percentage MULTIPLY, which is the only reason either number
matters. So the panel now names the unit, explains itself, and prints what each
step actually costs per iteration.

Two traps this file exists to catch:

  * the stylesheet uppercases <label>, so a sentence placed in one becomes a
    wall of capitals -- the first attempt did exactly that and read worse than
    the jargon it replaced;
  * the page offered "start from a Performance Test Plan" while that block is
    display:none, naming a route nobody could take.

Run:  ./.venv/bin/python tests_throughput_panel.py     # expect FAILURES: 0
"""
import io
import re
import sys

sys.path.insert(0, ".")

UI = io.open("ltmetrics/static/index.html", encoding="utf-8").read()

FAILURES = []

_FD_SRC = io.open("ltmetrics/agents/flow_discovery.py",
                  encoding="utf-8").read()


def check(name, cond, detail=""):
    print("  %-60s %s%s" % (name[:60], "ok" if cond else "FAIL",
                            "" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


def fn_body(name):
    """One top-level JS function's source. Sliced on the next top-level
    `function` -- brace counting walks off the end on regex literals."""
    start = UI.index("function %s(" % name)
    nxt = UI.find("\nfunction ", start + 1)
    body = UI[start:nxt if nxt > 0 else len(UI)]
    return body[:body.rindex("}") + 1]


def label_block():
    """The <label> for the panel, and the markup up to the panel itself."""
    i = UI.index("The test plan")
    j = UI.index('<div id="anThroughput"', i)
    return UI[UI.rindex("<label", 0, i + 1):j]


print("the panel says what it is")

blk = label_block()
lab = blk[blk.index(">") + 1:blk.index("</label>")]
check("the label is short enough to be shouted in capitals",
      len(lab.strip()) < 60, "it is %d chars" % len(lab.strip()))
check("the explanation is NOT inside the label",
      "multiply" not in lab and "request count is fixed" not in lab)
check("it lives in a hint paragraph instead",
      'class="hint"' in blk and "every virtual user walks that group" in blk)
# The sentence about the count multiplying went with the column it described --
# the canvas's row carries a name, a slider and a percentage. Comments are not
# copy, so they are stripped before reading what the panel actually says.
_said = re.sub(r"<!--.*?-->", "", blk, flags=re.S)
check("and says nothing about a count beside each row",
      "multiply" not in _said and "request count" not in _said)
check("and hint paragraphs are not uppercased",
      not re.search(r"p\.hint\{[^}]*text-transform", UI))
check("while labels are, which is why it had to move",
      re.search(r"\blabel\{[^}]*text-transform:uppercase", UI) is not None)

print()
print("a row is a name, a slider and a percentage -- the canvas's shape")
check("one bordered list, hairline between rows", ".thrList{border:1px solid" in UI)
check("the row carries the journey's severity bar",
      "border-left-color:var(--sev" in UI)
check("the slider is the canvas's 170px", ".thrRange{width:170px" in UI)
# The field rule outranks a class selector, so without excluding range the
# slider was stretched to its container and the names beside it wrapped.
check("and a range is not sized like a text field",
      "input:not([type=checkbox]):not([type=radio]):not([type=range]),select{" in UI
      and "input[type=range]{accent-color:var(--accent)}" in UI)
check("no per-row cost column", "thrEach" not in UI)

print()
print("a share can be set roughly or exactly")
# The canvas only offers the slider, at 5% steps. That cannot express 37%, so
# the step is 1 and the number box is back alongside it.
check("the slider moves a point at a time", 'step="1" value="100"' in UI)
check("and there is a box to type the number into",
      'class="thrNum"' in UI and 'type="number" min="0" max="100"' in UI)
# The field rule is input:not(...), which outranks a bare class whatever the
# source order -- unscoped, the box came out 937px wide.
check("the box is sized past the field rule, not by a bare class",
      "#anThroughput input.thrNum{width:84px" in UI)
# The native stepper is drawn inside the field's right edge, so a right-aligned
# value ran underneath it. The right padding is what the stepper sits in.
check("and the value clears its own stepper",
      "padding:0 24px 0 10px" in UI)
check("and the name keeps a floor so it is never crushed",
      ".thrName{font-size:14px;flex:1 1 auto;min-width:200px" in UI)
check("both are labelled for a screen reader",
      "share of traffic" in UI and "share, per cent" in UI)
check("one setter owns the value, so the two cannot disagree",
      "const _thrSet = (i, pct)=>" in UI
      and "if(r && +r.value !== pct) r.value = pct;" in UI
      and "if(n && +n.value !== pct) n.value = pct;" in UI)
check("it clamps what is typed rather than trusting it",
      "Math.max(0, Math.min(100, Math.round(+pct || 0)))" in UI)
check("a half-typed number is not corrected mid-keystroke",
      "if(el.value !== '') _thrSet" in UI and "el.onchange = ()=> _thrSet" in UI)
check("and the plan writes the box as well as the slider",
      "if(n) n.value=pct;" in UI)
# The total survives the row it used to sit beside: it is the only statement of
# what a full pass actually sends, and it carries the checkout warning.
check("there is still a total line", 'id="thrTotal"' in UI)
check("computed from count x percent", "g.count * pct / 100" in UI)

print()
print("the read-outs cannot go stale")
_hooks = UI.count("renderThroughputCost()")
# Both controls go through _thrSet, so the refresh sits there once rather than
# being repeated in each handler.
_set = UI[UI.index("const _thrSet = (i, pct)=>"):]
_set = _set[:_set.index(chr(10) + "  };") + 5]
check("the shared setter refreshes them", "renderThroughputCost();" in _set)
check("and both controls go through it",
      "_thrSet(+el.dataset.i, el.value)" in UI
      and UI.count("_thrSet(+el.dataset.i") >= 3)
check("so does typing a percentage", _hooks >= 3, "only %d call sites" % _hooks)
check("and they are painted before anything is touched",
      "renderThroughputCost();                    // paint" in UI
      or "// paint the costs before anything is touched" in UI)

print()
print("nothing promises a route the operator cannot take")
_visible = re.sub(r"//[^\n]*", "", UI)          # drop developer comments
_visible = re.sub(r"/\*.*?\*/", "", _visible, flags=re.S)
check("the old JMeter phrasing is gone from the copy",
      "Percent Executions" not in _visible)
check("but is kept in a comment, where it belongs",
      "Percent Executions" in UI)
check("the panel no longer claims a test plan adjusts it",
      "Auto-adjusted from the test plan" not in UI)
check("section 2 no longer offers the hidden test-plan upload",
      "or start from a Performance Test Plan and it fills these in" not in UI)
check("that block really is hidden, which is why the offer had to go",
      "Hidden per request: Performance Test Plan" in UI)

print()
print("a rare checkout is called out")
check("it warns when checkout seldom runs", "1 iteration in" in UI)
check("and when it never runs", "no orders are placed" in UI)


# --------------------------------------------------------------------------
# The total counts the RECORDING, not the run.
#
# The operator noticed the arithmetic did not close: the panel said 40.0 while
# the calls list said "10 of 16 selected". Both were right and neither matched
# what the test sent -- 18.8 requests per lap on their 5-user run.
#
#   40  every step in the recorded journey, across all six groups
#   16  the REST/API subset offered for ticking
#   10  the ones actually ticked
#   18.8  what the generated script sent per lap
#
# The count is honest; "on average per iteration" was not, because it reads as
# a prediction about the run. Say which of the four numbers this is.
# --------------------------------------------------------------------------
print()
print("the total does not pretend to predict the run")
_dl = fn_body("renderThroughputCost")
check("it names the recorded journey",
      "per full pass of the" in _dl and "recorded journey" in _dl)
check("and says the run sends fewer",
      "usually sends fewer" in _dl)
check("crediting both reasons: the ticked calls and correlation",
      "ticked" in _dl and "correlates" in _dl)
check("the misleading phrasing is gone",
      "on average per iteration" not in UI)

print()
print("a recording that names nothing still gets real groups")
# Amneal's recorder wrapped all 195 requests in one transaction it called
# "Test" and labelled every nested request with its own URL, so the panel
# offered a single meaningless slider. There was nothing to expand into: the
# stages had to come from what each call does.
import sys as _sys
_sys.path.insert(0, ".")
from ltmetrics.agents import flow_discovery as _fd


def _flow(*paths):
    return [{"path": p} for p in paths]


_AMNEAL = _flow(
    "/amnealajaxlogin/account/login",
    "/buy/product/429",
    "/rest/default/V1/carts/mine/shipping-information",
    "/rest/default/V1/carts/mine/payment-information",
    "/customer/section/load/",
    "/amnealcustomer/addressSelection/popupData",
    "//graphql",          # the one call in the real recording nothing can name
)
_g = _fd.api_call_groups(_AMNEAL)
_names = [x["name"] for x in _g]
check("one useless wrapper is not accepted as a group",
      not _fd._is_useful_group_name("Test"))
check("nor a label that is really a URL",
      not _fd._is_useful_group_name("https://shop.example.com/customer/section/load/"))
check("the stages are recovered from the calls",
      set(["Login", "Product View", "Shipping", "Payment / Place Order"])
      <= set(_names))
check("and it is marked as worked out, not recorded",
      all(x["derived"] for x in _g))
check("every step is accounted for",
      sum(x["count"] for x in _g) == len(_AMNEAL))
check("background traffic is a group of its own",
      "Background traffic" in _names)
check("and it comes last, after the real steps",
      _names.index("Background traffic") == len(_names) - 1)
check("a call the stage rules cannot name is named from its URL",
      "Graphql" in _names and "Unrecognised calls" not in _names)
check("the every-page ajax is one of them",
      _fd.is_every_page_call({"path": "/customer/section/load/"}))
check("a checkout call is not",
      not _fd.is_every_page_call({"path": "/amnealcustomer/addressSelection/popupData"}))
check("and the address picker is named, not left in a bin",
      "Checkout" in _names)

print()
print("an application that is not a shop still gets real groups")
# The stage rules are e-commerce vocabulary. A logistics journey matches none
# of it, and used to produce no groups at all -- nine calls, nothing on screen.
_FEDEX = _flow("/auth/oauth/v2/token", "/track/v1/trackingnumbers",
               "/rate/v1/rates/quotes", "/api/session/refresh")
_f = [x["name"] for x in _fd.api_call_groups(_FEDEX)]
check("every call is grouped", sum(x["count"] for x in _fd.api_call_groups(_FEDEX))
      == len(_FEDEX))
check("named from the part of the URL that says what it is for",
      _f == ["Auth", "Track", "Rate", "Session"])
check("version markers are not group names",
      _fd.path_group({"path": "/track/v1/trackingnumbers"}) == "Track")
check("nor is the transport prefix",
      _fd.path_group({"path": "/api/session/refresh"}) == "Session")
check("a recording that names its own steps still wins",
      [x["name"] for x in _fd.api_call_groups(
          [{"path": "/track/v1/x", "group": "Track shipment"},
           {"path": "/rate/v1/y", "group": "Get a rate"}])]
      == ["Track shipment", "Get a rate"])

print()
print("a recording that DOES name its steps keeps its own names")
_RADWELL = [{"path": "/customer/account/loginPost/", "group": "Login"},
            {"path": "/checkout/cart/add/", "group": "Add to cart"},
            {"path": "/rest/V1/carts/mine/payment-information", "group": "Checkout"}]
_r = _fd.api_call_groups(_RADWELL)
check("its own transaction names win",
      [x["name"] for x in _r] == ["Login", "Add to cart", "Checkout"])
check("nothing is marked as worked out", not any(x["derived"] for x in _r))
check("and neither remainder appears",
      not any(x["name"] in ("Unrecognised calls", "Background traffic")
              for x in _r))

print()
print("the journey and the traffic panel share one classifier")
# They used to be two copies of the same if/elif chain, free to drift apart and
# name the same call two different things.
check("the journey asks the classifier",
      "stage_of_step(s, milestones_only=True)" in _FD_SRC)
check("so do the groups", "stage_of_step(step)[0]" in _FD_SRC)
_name, _ms = _fd._derive_journey(_AMNEAL)
check("the journey still names itself from the same pass", _name == "Checkout")
check("and lists the stages it saw",
      _ms == ["Login", "Product View", "Shipping", "Payment / Place Order"])

print()
print("a supporting call is grouped without rewriting the journey")
# Marking cart-totals a milestone put "Cart" after "Shipping" on a store that
# reads totals late. Grouping it is useful; naming it in the journey was not.
_TOTALS = [{"path": "/rest/V1/carts/mine/totals"}]
check("it still gets a stage for grouping",
      _fd.stage_of_step(_TOTALS[0])[0] == "Cart")
check("but the journey does not name it",
      _fd.stage_of_step(_TOTALS[0], milestones_only=True)[0] == "")
check("so a late totals read cannot reorder the milestones",
      _fd._derive_journey(_AMNEAL + _TOTALS)[1] == _ms)

print()
print("the rules live in the knowledge base, with the code as a fallback")
check("they are read from the KB", "KB.platform_rules(\"generic\")" in _FD_SRC)
check("a KB that fails to load does not take the classifier with it",
      "except Exception:" in _FD_SRC and "return [(n, m, pr, True)" in _FD_SRC)
_kb = io.open("ltmetrics/knowledge/rules/platform_rules.yaml",
              encoding="utf-8").read()
check("the stages are defined there", "journey_stages:" in _kb)
check("so are the every-page calls", "every_page_calls:" in _kb)
check("and the milestone flag is used, not just declared",
      "milestone: false" in _kb)

print()
print("the noise filter keeps the journey, not a class of request")
# "Only REST & API" ticked a call on its TRANSPORT. That kept a promo-banner
# loader (tagged API) and dropped the purchase-order save, the address picker
# and the stock check, which are ordinary storefront POSTs.
_UI = io.open("ltmetrics/static/index.html", encoding="utf-8").read()
check("the row carries what the call does", 'data-stage="${_escAttr(c.stage' in _UI)
check("and whether it is background", "data-background=" in _UI)
check("background is dropped whatever it is tagged",
      "if(el.dataset.background === '1'){ el.checked = false; return; }" in _UI)
check("a named step is kept whatever it is tagged",
      "el.checked = !!el.dataset.stage || k==='REST' || k==='API'" in _UI)
_SRV = io.open("ltmetrics/server.py", encoding="utf-8").read()
check("the server computes both, so the page holds no second copy of the rule",
      "recording_agent.stage_of_step(s)[0]" in _SRV
      and "recording_agent.is_every_page_call(s)" in _SRV)

print()
print("an add-to-cart is not a page view")
# Magento's storefront add is /checkout/cart/add/uenc/<blob>/product/429/ --
# it contains "/product/" too, and Product View was listed first, so every add
# in every Magento recording was classified as a page view.
check("the add wins on the more specific match",
      _fd.stage_of_step({"path": "/checkout/cart/add/uenc/aB/product/429/"})[0]
      == "Add to Cart")
check("a real product view is untouched",
      _fd.stage_of_step({"path": "/catalog/product/view/id/429"})[0]
      == "Product View")
check("and the order is recorded where it matters",
      "Before Product View on purpose" in io.open(
          "ltmetrics/knowledge/rules/platform_rules.yaml",
          encoding="utf-8").read())

print()
print("FAILURES: %d" % len(FAILURES))
sys.exit(1 if FAILURES else 0)

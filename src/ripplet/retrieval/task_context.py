"""task_context: the smallest evidence bundle to start a task.

Given a task in plain words ("add a due date to tasks and show it in the
list", "fix the 500 when a user has no avatar"), one call returns what an
agent would otherwise collect over many turns:

  * the code symbols the task is about, ranked (name parts, path, usage),
    each with its signature line, location, a short source excerpt and the
    places that call it (with the call's code)
  * related entry points (routes) and database tables/columns
  * an existing feature to imitate when the task says "like X" / "similar
    to X", as its file list (feature_slice gives the full files)
  * the files most likely to be edited
  * the tests that reach those symbols, with the commands to run them

Ranking is lexical + structural and fully deterministic; nothing is
inferred that the index does not contain. When nothing matches, it says so.
"""
from __future__ import annotations

import math
import re
import time
from collections import defaultdict
from pathlib import Path

_CODE = ("function", "method", "class", "interface", "struct", "type", "trait", "enum", "record")
_STOP = set("""a an the to for of in on at by with and or not no is are be been being it its this that these those
then than when while where which what who whom how why should would could can may might must will shall do does
did done make makes made add adds added fix fixes fixed bug bugs issue issues support supports implement implements
change changes update updates new old use uses using via into from as so if else also only just like similar same
more less all any each every some please need needs want wants show shows list get gets set sets there their
them they we you your our my me i one two first last way thing things code feature work works
working how what does existing current""".split())
_TEST_RX = re.compile(r"(^|/)(tests?|spec|__tests__)/|(_test|_spec|\.test|\.spec|Tests?)\.[a-z]+$|"
                      r"(^|/)test_[^/]*$", re.I)


def _stem(w: str) -> str:
    w = w.lower()
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"                          # categories -> category
    if len(w) > 4 and w.endswith(("sses", "shes", "ches", "xes", "zes")):
        return w[:-2]                                # addresses -> address, boxes -> box
    if len(w) > 3 and w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]                                # invoices -> invoice, status stays
    return w


# the stop list holds plain words; terms are compared after stemming (`does` -> `doe`)
_STOP |= {_stem(w) for w in _STOP}


def _parts(name: str) -> list:
    """`getDueDate` / `due_date` / `DueDateField` -> [get, due, date, ...] (stemmed)."""
    raw = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name or "")
    return [_stem(x) for x in re.split(r"[^A-Za-z0-9]+", raw) if len(x) >= 2]


# Tasks are often written in the user's language while code is English. A
# Turkish word maps to the identifiers it usually means (matched by prefix, so
# suffixes like görev-in / görev-ler / taşı-n-dığında still match).
_TR_CODE = {
    "görev": ["task"], "iş": ["task"], "durum": ["status"], "statü": ["status"], "kolon": ["column", "status"],
    "sütun": ["column"], "taşı": ["move"], "sürükle": ["move", "drag"], "bırak": ["drop"], "sıra": ["order", "sort"],
    "kullanıcı": ["user"], "üye": ["member"], "proje": ["project"], "bildirim": ["notification", "notify"],
    "yorum": ["comment"], "etiket": ["label"], "sayfa": ["page"], "yetki": ["role", "permission"],
    "rol": ["role"], "rolü": ["role"], "rolün": ["role"], "değiş": ["change", "update"], "izleyici": ["viewer"], "çalışma": ["workspace"], "alan": ["field"], "tarih": ["date"],
    "bitiş": ["due"], "başlangıç": ["start"], "süre": ["time", "estimate"], "tahmin": ["estimate"],
    "ekle": ["add", "create"], "oluştur": ["create"], "sil": ["delete"], "güncelle": ["update"],
    "düzenle": ["edit", "update"], "kaydet": ["save", "store"], "giriş": ["login"], "şifre": ["password"],
    "parola": ["password"], "dosya": ["file"], "ek": ["attachment"], "mesaj": ["message"],
    "bağımlı": ["dependency", "blocking"], "engel": ["block", "blocked"], "otomasyon": ["automation"],
    "kural": ["rule"], "aktivite": ["activity"], "etkinlik": ["activity"], "rapor": ["report"],
    "takvim": ["calendar"], "şablon": ["template"], "ara": ["search"], "arama": ["search"], "pano": ["board"],
    "tamamla": ["done", "complete"], "atanan": ["assignee"], "ata": ["assign"], "davet": ["invite"],
    "oturum": ["session"], "e-posta": ["email"], "eposta": ["email"], "hata": ["error"], "sorgu": ["query"],
    "tablo": ["table"], "kolonu": ["column"], "yineleyen": ["recurrence", "recurring"], "tekrar": ["recurrence"],
    "döngü": ["cycle"], "zaman": ["time"], "kayıt": ["log", "entry"], "geçmiş": ["history", "version"],
    "paylaş": ["share"], "abonelik": ["subscription"], "webhook": ["webhook"], "entegrasyon": ["integration"],
}
_TR_STOP = set("""bir ve ile için bu şu o olan gibi daha çok ne neler nasıl hangi var yok olarak sonra önce kadar
değil mi mı mu mü da de ya veya ama fakat ise eğer her hiç başka diğer tüm bütün sadece yalnızca zaten
şey şekilde içinde üzerinde arasında yani hem gerek gerekiyor lazım olsun oluyor olur olabilir""".split())


def _tr_terms(tok: str) -> list:
    low = tok.lower()
    if low in _TR_STOP:
        return []
    best = max((k for k in _TR_CODE if low.startswith(k) and (len(k) >= 4 or low == k or len(low) <= len(k) + 3)),
               key=len, default=None)
    return list(_TR_CODE[best]) if best else []


def task_terms(task: str) -> list:
    """Significant stemmed words of the task, identifiers split, in order.
    Words are Unicode (`sürüklendiğinde` stays one word, not `klendi`+`inde`);
    a non-English word contributes the code words it maps to, never fragments."""
    out = []
    for tok in re.findall(r"[^\W\d][\w-]*", task or ""):
        if not tok.isascii() or tok.lower() in _TR_STOP or _tr_terms(tok):
            parts = _tr_terms(tok)
        else:
            parts = _parts(tok)
        for p in parts:
            if p not in _STOP and len(p) >= 3 and p not in out:
                out.append(p)
    return out[:12]


_AUX_RX = re.compile(r"(^|/)(examples?|samples?|demos?|performance|benchmarks?|bench|docs?|scripts?|"
                     r"tools|contrib|fixtures)/", re.I)


def task_idents(task: str) -> list:
    """Names written as code in the task: `OneOf.options()`, `TimeDelta`,
    `from_timestamp_ms`, `marshmallow.validate` -> their dotted parts."""
    out = []
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", task or ""):
        code_like = "." in tok or "_" in tok.strip("_") or re.search(r"[a-z][A-Z]|^[A-Z][a-z]+[A-Z]", tok)             or (re.fullmatch(r"[A-Z][a-z0-9]+", tok) and tok.lower() not in _STOP and
                not task.strip().startswith(tok))
        if code_like:
            out.append(tok)
    return out


def _like_target(task: str) -> str:
    """`like favorites` / `similar to how notifications work` -> the feature word."""
    m = re.search(r"\b(?:like|similar to|same as|just as|mirroring|modeled on|the way)\s+(.{0,60})", task or "", re.I)
    if not m:
        return ""
    for w in re.findall(r"[A-Za-z_][\w-]*", m.group(1)):
        if w.lower() not in _STOP:
            return w
    return ""


def task_files(store, cfg, task: str, limit: int = 10) -> list:
    """The files task_context would send an agent to edit, in order."""
    r = task_context(store, cfg, task, _files_only=True)
    return r[:limit] if isinstance(r, list) else []


def task_context(store, cfg, task: str, max_symbols: int = 6, excerpt_lines: int = 12,
                 _files_only: bool = False, _meta: dict | None = None):
    cx = store.cx
    roots = [str(r) for r in (getattr(cfg, "workspace_roots", None) or ["."])]
    terms = task_terms(task)
    if not terms:
        if _files_only:
            return []
        return "task_context: no significant words in the task. Name the feature, entity or error."
    tset = set(terms)
    idents = task_idents(task)
    exact = {p for i in idents for p in i.split(".")}
    dotted = {tuple(i.split(".")[-2:]) for i in idents if "." in i}
    # `... message in CodeWriter`, `... to Schema.TYPE_MAPPING`: a name after a
    # place word says WHERE the change goes, not what it is about
    located = {m.group(1).split(".")[0] for m in re.finditer(
        r"\b(?:in|into|inside|within|on|from|to)\s+`?([A-Za-z_][A-Za-z0-9_.]*)", task or "")} & exact
    q = ",".join("?" * len(_CODE))
    users = defaultdict(int)
    for t, c in cx.execute("SELECT target, COUNT(DISTINCT source) FROM edges WHERE relation IN "
                           "('calls','instantiates','references','dispatches') GROUP BY target"):
        users[t] = c
    rows = cx.execute(f"SELECT id, type, label, qualified_name, file_path, start_line, end_line FROM nodes "
                      f"WHERE type IN ({q}) AND file_path<>''", _CODE).fetchall()
    # rarer words say more: `nested`, `partial` beat `field` in a repo of fields
    df = defaultdict(int)
    for r in rows:
        for t in set(_parts(r[2])) & tset:
            df[t] += 1
    w = {t: 1.0 + math.log((1 + len(rows)) / (1 + df[t])) / 3 for t in tset}
    # a named type the repo does not define (`InetAddress`, `Serializable`,
    # `LinkedHashMap`): the code to change is where it is used. Only when few
    # files use it; `String` everywhere says nothing.
    defined = {r[2] for r in rows}
    uses_ext: dict = defaultdict(int)
    for name in sorted(exact - defined):
        if len(name) < 3:
            continue
        files = {fp for (fp,) in cx.execute(
            "SELECT DISTINCT file_path FROM edges WHERE file_path<>'' AND (target LIKE ? OR target LIKE ? "
            "OR target LIKE ?)", (f"%.{name}", f"%:{name}", f"%:{name}.%"))
            if not _TEST_RX.search(fp)}
        if 1 <= len(files) <= 8:
            for fp in files:
                uses_ext[fp] += 1
    # A name the task writes as code that no symbol carries (`explain_symbol`: a string a dispatcher
    # branches on): the code to change is where that string is used. Only when a few places use it.
    sites: dict = {}
    lit_names = [n for n in sorted(exact - defined)
                 if len(n) >= 5 and ("_" in n.strip("_") or re.search(r"[a-z][A-Z]", n))][:3]
    if lit_names:
        rows_by_file = defaultdict(list)
        for r_ in rows:
            rows_by_file[r_[4]].append(r_)
        lit_rx = {n: re.compile(r"""["'`]""" + re.escape(n) + r"""["'`]""") for n in lit_names}
        lit_found: dict = defaultdict(dict)
        deadline = time.monotonic() + 1.0       # a huge repo must not make this call slow
        for fp_, syms in rows_by_file.items():
            if time.monotonic() > deadline:
                break
            if _TEST_RX.search(fp_ or ""):
                continue
            text = ""
            for root_ in roots:
                path_ = Path(root_) / fp_
                if path_.is_file():
                    try:
                        if path_.stat().st_size <= 300_000:
                            text = path_.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        pass
                    break
            if not text:
                continue
            for n, rx in lit_rx.items():
                for m in rx.finditer(text):
                    line = text.count("\n", 0, m.start()) + 1
                    inside = [s for s in syms if s[5] and s[5] <= line <= (s[6] or s[5])]
                    if inside:
                        lit_found[n][min(inside, key=lambda s: (s[6] or s[5]) - s[5])[0]] = 1
        for ids in lit_found.values():
            if 1 <= len(ids) <= 5:
                for i in ids:
                    sites[i] = sites.get(i, 0) + 1
    # Symbols between two things the task names (`_resolve`: called by the dispatcher the task points at,
    # and calls `search_symbols`, which it names) are where such a change usually goes.
    by_label = defaultdict(list)
    for r_ in rows:
        if r_[2] in exact and len(r_[2]) >= 5:
            by_label[r_[2]].append(r_[0])
    anchors = {i for ids in by_label.values() if len(ids) <= 3 for i in ids} | set(sites)
    anc = sorted(anchors)[:40]
    near: dict = defaultdict(set)
    if anc:
        ph = ",".join("?" * len(anc))
        for s_, t_ in cx.execute(
                f"SELECT source, target FROM edges WHERE relation IN ('calls','instantiates','references','dispatches') "
                f"AND (source IN ({ph}) OR target IN ({ph}))", anc + anc):
            if s_ in anchors:
                near[t_].add(s_)
            if t_ in anchors:
                near[s_].add(t_)
    # a call on a receiver the index could not type (`store.search_symbols(..)`) is not an edge, but the
    # method name it spells is still a weak tie to the symbol the task names
    for lab, ids in by_label.items():
        if len(ids) <= 3:
            for (s_,) in cx.execute("SELECT DISTINCT source FROM edges WHERE relation='calls_unresolved' "
                                    "AND (target LIKE ? OR target LIKE ?) LIMIT 400", (f"%.{lab}", f"%:{lab}")):
                near[s_].add("~" + lab)
    bridge = {i: len(a) for i, a in near.items() if i not in anchors and len(a) >= 2}
    scored = []
    for nid, typ, lab, qn, fp, sl, el in rows:
        lp = set(_parts(lab))
        qp = set(_parts(qn)) - lp
        fpp = set(_parts(Path(fp or "").stem)) | set(_parts(Path(fp or "").parent.name))
        hit_l = sum(w[t] for t in lp & tset)
        hit_q = sum(w[t] for t in qp & tset)
        hit_f = sum(w[t] for t in fpp & tset)
        is_exact = lab in exact
        mod = (fp or "").rsplit(".", 1)[0].replace("/", ".")
        mod_hit = any(mod.endswith(".".join(d)) for d in dotted)
        ext_use = min(uses_ext.get(fp, 0), 2)
        site = sites.get(nid, 0)
        br = bridge.get(nid, 0)
        if not (hit_l or hit_q or hit_f or is_exact or mod_hit or ext_use or site or br):
            continue
        cover = len((lp | qp | fpp) & tset)
        score = 3.0 * hit_l + 1.5 * hit_q + 1.0 * hit_f + 2.0 * (cover - 1) + 0.6 * math.log1p(users.get(nid, 0))
        if is_exact:
            # the task names this symbol as code; `Owner.member` naming both is surer still
            owner = qn.rsplit(".", 2)[-2] if qn.count(".") >= 1 else ""
            score += 8.0 + (4.0 if any(d == (owner, lab) or d[0] == lab for d in dotted) else 0.0)
            if lab in located and typ in ("class", "interface", "struct", "record", "trait", "enum"):
                score += 8.0
        if mod_hit:
            score += 6.0
        score += 8.0 * ext_use
        if site:
            score += 9.0
        if br:
            score += min(5.0 + 2.0 * (br - 2), 9.0)
        if _AUX_RX.search(fp or ""):
            score -= 3.0
        if typ in ("class", "interface", "struct", "record", "trait"):
            score += 0.3
        test = bool(_TEST_RX.search(fp or ""))
        scored.append((score - (4.0 if test else 0.0), nid, typ, lab, qn, fp, sl or 0, el or sl or 0, test, cover))
    cache: dict = {}

    def lines_of(rel):
        if rel not in cache:
            cache[rel] = []
            for r in roots:
                p = Path(r) / rel
                if p.is_file():
                    try:
                        cache[rel] = p.read_text(encoding="utf-8", errors="replace").splitlines()
                    except OSError:
                        pass
                    break
        return cache[rel]

    scored.sort(key=lambda x: (-x[0], x[4]))
    picked, per_file = [], defaultdict(int)
    for row in scored:
        if row[8]:
            continue
        if per_file[row[5]] >= 2:
            continue
        picked.append(row)
        per_file[row[5]] += 1
        if len(picked) >= max_symbols:
            break

    out = [f"# task_context: {task.strip()[:160]}",
           f"Matched on: {', '.join(terms)}. {len(scored)} candidate symbols; the strongest below.",
           "Read these first; explain_symbol / change_plan / feature_slice go deeper on any of them.", ""]
    if not picked and _files_only:
        return []
    if not picked:
        out.append("No code symbol matches the task's words. Try the domain words the code uses, "
                   "or repo_map to see what exists.")
        # a vague task ("fix typos", "remove unused ignore") still lands somewhere:
        # name the library's central files, clearly marked as a fallback
        hubs = defaultdict(int)
        for nid, fp in cx.execute(f"SELECT id, file_path FROM nodes WHERE type IN ({q}) AND file_path<>''", _CODE):
            if not _TEST_RX.search(fp) and not _AUX_RX.search(fp):
                hubs[fp] += users.get(nid, 0)
        top = sorted(hubs, key=lambda f: (-hubs[f], f))[:5]
        if top:
            out.append("")
            out.append("=== No match; the most used files of the codebase (a guess, not evidence)")
            out.extend(f"  {f}  [{hubs[f]} uses]" for f in top)
        return "\n".join(out)
    # each symbol's file, then the file that calls it most: a changed contract
    # is usually fixed at its caller too
    # a file is as relevant as its best symbol plus a share of its other
    # matches: the central file many matching symbols live in beats a small
    # file with one lucky name
    per_file_scores: dict = defaultdict(list)
    for row in scored:
        if not row[8]:
            per_file_scores[row[5]].append(row[0])
    fscore = {f: v[0] + 0.25 * sum(sorted(v, reverse=True)[1:5])
              for f, v in ((f, sorted(v, reverse=True)) for f, v in per_file_scores.items())}
    picked = sorted(picked, key=lambda r: (-fscore.get(r[5], 0.0), -r[0]))
    if _meta is not None:
        # how sure the match is, for callers that must decide whether to show it at all
        _meta.update(top_score=round(max(r[0] for r in picked), 2), top_cover=max(r[9] for r in picked),
                     named=len(exact & {r[3] for r in picked}), picked=len(picked))
    # Calls on a receiver the index could not type are not edges, so a helper like `getRetryInterval` shows
    # "used by 0" while the loop that answers the task calls it. Show those sites, marked as unproven.
    possible: dict = {}
    from .uncertain import TOO_COMMON, unverified_sites
    for row in picked:
        if row[2] in ("function", "method") and users.get(row[1], 0) < 3:
            try:
                psites, ptotal = unverified_sites(cx, roots, {row[1]}, row[3], limit=3)
            except Exception:  # noqa: BLE001
                continue
            if 0 < ptotal <= TOO_COMMON:
                possible[row[1]] = (psites, ptotal)
    edit = []
    for row in picked:
        if row[5] not in edit:
            edit.append(row[5])
        found_caller = False
        for cf, _n in cx.execute("SELECT file_path, COUNT(*) c FROM edges WHERE target=? AND relation IN "
                                 "('calls','instantiates','references') GROUP BY file_path "
                                 "ORDER BY c DESC LIMIT 6", (row[1],)):
            if cf and cf not in edit and not _TEST_RX.search(cf) and not _AUX_RX.search(cf):
                edit.append(cf)
                found_caller = True
                break
        if not found_caller and row[1] in possible:
            for pf, _x, _code, _r in possible[row[1]][0]:
                if pf and pf not in edit and not _TEST_RX.search(pf) and not _AUX_RX.search(pf):
                    edit.append(pf)
                    break
    if _files_only:
        return edit
    out.append("=== Files you will most likely edit")
    out.extend(f"  {f}" for f in edit[:10])
    out.append("")
    from ..change.check import site_line
    out.append(f"=== Most relevant code ({len(picked)})")
    shown_possible: set = set()
    for score, nid, typ, lab, qn, fp, sl, el, _t, cover in picked:
        lines = lines_of(fp)
        head = lines[sl - 1].strip() if 1 <= sl <= len(lines) else ""
        psites, ptotal = possible.get(nid, ([], 0))
        used = f"used by {users.get(nid, 0)}" + (f" proven, {ptotal} possible" if ptotal else "")
        out.append(f"-- {qn} ({typ}) @ {fp}:{sl}-{el}  [{used}]")
        if head:
            out.append(f"   {head[:160]}")
        body_end = min(el, sl + excerpt_lines - 1)
        if el > sl and lines:
            out.extend(f"   {i:>5}| {lines[i - 1]}" for i in range(sl + 1, min(body_end, len(lines)) + 1))
            if el > body_end:
                out.append(f"   ... {el - body_end} more lines (explain_symbol for all of it)")
        callers = cx.execute(
            "SELECT n.qualified_name, e.file_path, e.start_line FROM edges e JOIN nodes n ON n.id=e.source "
            "WHERE e.target=? AND e.relation IN ('calls','instantiates','references') "
            "AND e.id NOT IN (SELECT edge_id FROM superseded_edges) LIMIT 40", (nid,)).fetchall()
        callers = sorted(callers, key=lambda r: bool(_TEST_RX.search(r[1] or "")))[:3]
        for cq, cf, cl in callers:
            site = site_line(roots, cf, cl or 0, lab) if cf else None
            code = f" :: {site[1]}" if site else ""
            out.append(f"   <- {cq} @ {cf}:{site[0] if site else cl}{code}")
        for pf, px, pcode, _r in psites:
            if (pf, px) in shown_possible:
                continue                    # the same site under a same-named method above
            shown_possible.add((pf, px))
            holder = cx.execute("SELECT qualified_name FROM nodes WHERE file_path=? AND start_line<=? AND end_line>=? "
                                "AND type IN ('function','method') ORDER BY end_line-start_line LIMIT 1",
                                (pf, px, px)).fetchone()
            inside = f" (in {holder[0].rsplit('.', 1)[-1]})" if holder else ""
            out.append(f"   <~ possible caller (receiver type unknown) @ {pf}:{px}{inside} :: {pcode}")
    out.append("")
    # routes and schema the task names
    routes = []
    for lab, fp, sl in cx.execute("SELECT n.label, n.file_path, n.start_line FROM nodes n WHERE n.type IN "
                                  "('route','api_handler')"):
        if set(_parts(lab)) & tset:
            routes.append(f"  {lab}  @ {fp}:{sl}")
    tables = []
    trows = []
    for tid, lab, fp, sl in cx.execute("SELECT id, label, file_path, start_line FROM nodes WHERE type='table'"):
        hit = len(set(_parts(lab)) & tset)
        if hit:
            trows.append((-hit, len(_parts(lab)) - hit, tid, lab, fp, sl))
    # the table the task names most exactly first (`tasks` before `task_labels`)
    for _h, _extra, tid, lab, fp, sl in sorted(trows)[:6]:
        n_users = cx.execute("SELECT COUNT(DISTINCT source) FROM edges WHERE target=? AND method='sql-link' "
                             "AND json_extract(attributes,'$.via') IN ('sql-literal','orm')", (tid,)).fetchone()[0]
        cols = [r[0] for r in cx.execute("SELECT label FROM nodes WHERE type='column' "
                                         "AND json_extract(attributes,'$.table')=? LIMIT 30", (lab,))]
        tables.append(f"  {lab} @ {fp}:{sl}  [{n_users} code users]  columns: {', '.join(cols)[:200]}")
    if routes:
        out.append(f"=== Entry points ({len(routes)})")
        out.extend(routes[:8])
        out.append("")
    if tables:
        out.append(f"=== Database tables ({len(tables)})")
        out.extend(tables[:6])
        out.append("")
    like = _like_target(task)
    like_files = []
    if like:
        from .feature_slice import feature_slice
        fs = feature_slice(store, cfg, like, files_only=True)
        if isinstance(fs, list):
            like_files = [(k, f) for k, f in fs if k in ("IMPLEMENTS", "TOUCH POINT")]
        if like_files:
            out.append(f"=== Existing feature to imitate: {like!r} (feature_slice({like!r}) returns the full files)")
            out.extend(f"  {kind.lower()}: {f}" for kind, f in like_files[:14])
            out.append("")
    more = [f for kind, f in like_files if kind == "TOUCH POINT" and f not in edit]
    if more:
        out.append("=== Also touch (from the feature to imitate)")
        out.extend(f"  {f}" for f in more[:6])
        out.append("")
    # tests that reach the picked symbols
    from ..change.check import _G, _test_commands, _tests
    g = _G(cx)
    reach = _tests(g, [r[1] for r in picked], depth=4)
    if reach:
        tc = _test_commands(g, reach, roots)
        out.append(f"=== Tests that reach this code ({tc.get('total', len(reach))})")
        for x in (tc.get("files") or [])[:8]:
            out.append(f"  {x['file']}: {', '.join(x['tests'][:4])}")
        for cmd in (tc.get("commands") or [])[:2]:
            out.append(f"  run: {cmd}")
    else:
        out.append("=== Tests: none reach this code yet; add one next to the code you change.")
    out.append("")
    out.append("After editing: check_change (what you missed, tests to run).")
    return "\n".join(out)

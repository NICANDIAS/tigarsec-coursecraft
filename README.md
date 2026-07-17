# TIGARSEC Assess — v2

A rebuild addressing the round of testing feedback. Quick map of what changed, numbered
to match the feedback list:

1. **Sidebar UI** — full-screen app shell with a left sidebar (Dashboard, Spec Library,
   Analytics, Internal Verification, Admin, Settings), room to add more buttons as features grow.
2/5/7/9/14. **Parsing hangs, silent failures, 1000%+ CPU, "0 units", 500 errors** — all one
   root cause: sending a full 50-100 page spec to a small model in one blocking call. Fixed by:
   - **chunking** the document before sending it to the LLM (`app/spec_parser.py`), so each
     call is small and reliable instead of one giant, fragile generation
   - **background processing** (spec parsing and marking both run in the background now;
     the page shows live progress via polling instead of hanging with no feedback)
   - **proper error capture** — failures are caught, stored, and shown in the UI instead of
     surfacing as a raw 500
   - **Docker CPU/memory limits** on the Ollama and app containers, so local inference can't
     consume the whole machine (see the note in `docker-compose.yml` — this caps usage, it
     doesn't make local CPU inference fast; that's inherent to running an LLM without a GPU)
3. **Logo + footer** — sidebar has a logo slot (drop a file at `app/static/img/logo.png` and
   it appears automatically; falls back to a text monogram until you do) plus a footer.
   Waiting on your actual brand images/colours/stripe pattern to finish this properly.
4. **Timetabling** — added as a "Coming soon" sidebar link per your request, not built yet.
6. **Scheme of work** — now takes a content-area focus, week count, and an optional upload
   of your college's own SOW format/template (used as a style guide, not copied literally).
   **Analytics dashboard** — new page with summary cards, two charts (status breakdown, mark
   distribution), and a table of which criteria get overridden most often.
8. **Student edit/archive** — students can be renamed/corrected, and archived instead of
   deleted (deleting would break their submission/marking history).
11. **Spec library** — specs are now uploaded once, centrally, by admin/curriculum_manager
   only. Everyone else attaches an existing spec to their course from a dropdown instead of
   re-uploading the same document.
12. **"Coming soon" placeholders** — Timetabling and Teams integration are visible in the
   sidebar now, clearly marked, rather than absent.
13. **Feedback style selector** — Settings now has a dropdown for "standard" vs "What Went
   Well / Even Better If" comment structure, which feeds directly into the marking prompt.
15. **Internal Verification (IV)** — new module: send an already-marked submission to a
   colleague for IV (AI prepares a first-pass suggestion per criterion to speed up their
   review), or upload work that was marked entirely outside this tool for AI-assisted IV.
16. **Answered below**, in "What 'Parse with AI' actually does."

---

## What "Parse with AI" actually does (item 16)

You upload the raw specification (PDF/docx/text) and the tool structures it for you — you
are not expected to manually structure it yourself. What you *should* do is treat the AI's
structuring as a first draft: after parsing, look over the units/LOs/criteria it extracted
before relying on it for marking, the same way you'd sanity-check any automated extraction
of an official document. If something's missing or misread, re-parsing after fixing the
source text (or switching to a better model) is the fix, not manual re-typing.

---

## Before you run this: your database needs to be reset

This version changed the database schema significantly (spec library instead of per-course
specs, new IV tables, student archive flag, teacher roles/feedback style). SQLAlchemy's
auto-create only creates missing *tables*, not new columns on ones that already exist, so:

```bash
rm -f data/tigarsec_assess.db
```

before starting this version. You'll need to re-register (first account becomes admin again)
and re-upload your spec into the new library. This is exactly the kind of pain Alembic
migrations exist to prevent — still on the priority list, see below.

---

## Running it

Same one-command setup as before:

```bash
cd tigarsec-assess-v2
docker compose up --build
```

Open **http://localhost:8000**. First account you register becomes the protected admin.

**Being realistic about speed:** even with chunking, a local CPU-only model is genuinely
slower than a hosted one — chunking makes it *reliable* (no more hangs/crashes/0-unit
results), not instant. For a full spec, expect it to work through several chunks
sequentially rather than one long freeze. If parsing speed still isn't acceptable for your
actual workflow once you're past testing the mechanics, that's the point to switch
`LLM_PROVIDER=anthropic` in `.env` — hosted inference doesn't have this ceiling.

---

## What's still not built (be aware before relying on this)

- **No database migrations (Alembic)** — schema changes still mean resetting the dev DB.
  This is the single biggest thing to fix before this holds data you can't afford to lose.
- **No real job queue (Redis/RQ)** — background tasks use FastAPI's built-in `BackgroundTasks`
  (a thread pool in the same process), which is a genuine improvement over blocking requests
  but won't scale gracefully to many teachers marking simultaneously on a shared server. Move
  to Redis+RQ before a multi-campus rollout, per the original architecture doc.
- **Analytics recalculates from scratch on every visit** rather than being pre-aggregated —
  fine at current scale, will need proper SQL aggregation (not Python-side loops) once there's
  real volume of submissions across many courses.
- **IV's "external upload" flow can't automatically read comments back out of an
  already-annotated Word document** — reading Word comments reliably is its own can of worms,
  so you paste a summary of the existing marks instead. Worth revisiting if this becomes a
  frequent workflow.
- **Packet Tracer / network config marking** is still just a dropdown option, not an actual
  marking module — deterministic config-checking (not text-based judgement) is still the
  planned approach, unchanged from before.
- **Brand assets** — logo/colour/stripe pattern are placeholders until you share the actual
  images; the CSS is built around variables specifically so that reskin will be quick once
  you do.

---

## Project structure (what's new vs before)

```
tigarsec-assess-v2/
├── app/
│   ├── spec_parser.py          NEW - chunked spec parsing + merge logic
│   ├── routers/
│   │   ├── specs.py             REWRITTEN - shared library, background parse, status polling
│   │   ├── submissions.py       REWRITTEN - background marking, feedback style, IV hookup
│   │   ├── scheme_of_work.py    REWRITTEN - content area focus, format sample upload
│   │   ├── students.py          UPDATED - edit + archive (no hard delete)
│   │   ├── analytics.py         NEW
│   │   ├── iv.py                NEW - internal verification, both flows
│   │   └── admin.py             unchanged from the previous role/account management update
│   └── templates/
│       ├── base.html             REWRITTEN - sidebar shell, logo slot, footer
│       ├── base_auth.html        NEW - non-sidebar layout for login/register
│       ├── analytics.html        NEW (Chart.js via CDN)
│       ├── iv_*.html              NEW
│       └── spec_library.html      NEW
├── docker-compose.yml            UPDATED - cpu/mem limits on both containers
└── (everything else follows the same structure as before)
```

## Priority order for next changes

1. Test the whole pipeline once more end to end on your own real spec PDF — confirm
   chunked parsing actually gives you real units this time (a fake test was run during
   development confirming the chunking/merge logic and the docx comment-annotation both
   work correctly with real python-docx, but a full real-spec run is worth doing yourself).
2. Alembic migrations, so future schema changes don't require wiping your data.
3. Move background jobs to Redis+RQ once more than one or two people are using this at once.
4. Send over your brand assets (logo, colours, stripe pattern) and I'll wire them into the CSS.
5. Build the Packet Tracer marking module as a config-diffing check, not a text judgement.

---

## Spec parsing now "learns" new formats (latest update)

Parsing order is now: **BTEC rules → T-Level rules → any previously learned patterns → AI
pattern discovery (once) → per-section AI fallback** (never the whole document at once,
unless truly nothing above matched).

The first time a genuinely new spec format shows up (not BTEC, not T-Level), the AI is asked
**once** to describe the document's structure as a reusable pattern. That pattern is saved to
the database (`discovered_spec_formats` table) and tried automatically on every future spec -
so you should only ever hit real AI-assisted parsing once per *format*, not once per document.
This was verified end-to-end with a mocked format: first upload cost one AI call and learned
the pattern; a second document of the same fictional format reused it with zero AI calls.

This is a genuinely new database table, but existing data is safe - it's a new table, not a
changed column, so **no database reset is needed** for this update (unlike the v1→v2 jump).

Also fixed: error messages on a failed parse now show the full technical detail (not just a
one-line summary) so if something does go wrong, it's actually diagnosable from the error box
on the spec page rather than a mystery. If "Parse with AI" still doesn't work after this
update, please paste the full text from that error box - it'll now show exactly where and why.

**Known limitation to be aware of:** AI-generated regex patterns are not sandboxed against
worst-case pathological inputs (a poorly-formed pattern could theoretically be slow on
adversarial text - "catastrophic backtracking"). Low risk for normal spec documents, but worth
knowing if this ever gets exposed beyond trusted internal use.

---

## Fetching a spec from a URL (latest update)

The spec upload form now has a third option alongside file upload and pasting text: **fetch
directly from a web link**. Works for a direct PDF/docx link or a general web page (HTML gets
stripped down to text automatically, no extra dependency needed for that).

**Security note, since this fetches a URL you provide from the server:** this is restricted to
admin/curriculum_manager (same as all spec uploads) and has real protections against SSRF -
only public http/https URLs are allowed; anything resolving to a private, loopback, link-local,
or reserved address (localhost, 127.0.0.1, 10.x/192.168.x internal ranges, the cloud metadata
endpoint 169.254.169.254, etc.) is blocked, verified with real tests including the metadata
endpoint specifically. There's also a 30MB size cap and a 30-second timeout so a slow or huge
response can't hang the upload.

---

## Lesson PowerPoint generation (latest update)

New "Lesson PowerPoints" section on each course page. Pick a content area from the attached
spec, set the lesson duration, optionally pull "what was covered last lesson" from your
scheme of work (or type it in), and generate.

**The pedagogical structure is fixed, not just AI improvising a deck:**
1. Title
2. Retrieval starter - recall questions about the PREVIOUS lesson (not today's), to activate
   prior learning before new content starts
3. Learning objectives for this lesson
4. Content taught through guiding questions posed to students first (student-led), with a
   check question per segment and a real-world image
5. Real-life example slides tying content to an actual scenario
6. Timed activities (and practicals, if enabled) - time allocated based on task complexity
7. Retrieval plenary - checking THIS lesson's content specifically
8. Objectives revisited with a self-assessment prompt per objective

**Images resolve through three tiers, always falling through gracefully:** a real photo via
Unsplash (if `UNSPLASH_ACCESS_KEY` is set in `.env` - free tier available, attribution added
automatically per their license terms) → a teacher-uploaded image from the shared Media
Library (tag-matched by keyword) → a clean generated placeholder graphic if neither is
available. A deck is never left with a blank gap where an image should be, and the detail
page tells you honestly how many slides used a generated fallback vs a real photo.

**This was genuinely tested, not just written:** the actual renderer was run against a
realistic hand-written lesson outline covering every slide type, converted to images with
LibreOffice, and visually inspected slide-by-slide. That process caught and fixed a real
layout bug (a fixed-position element that could have overlapped with longer AI-generated
content) before it ever reached you - re-verified with a stress test using longer text.
Content QA via `markitdown` confirmed no missing/garbled content and no leftover placeholder
text.

**Known limitations, honestly:**
- Time allocations are the AI's estimate, not validated against the stated lesson duration -
  if a generated lesson's activities clearly don't fit the time given, that's worth flagging
  back so the prompt can be tightened.
- The "previous lesson" auto-suggestion only offers the first scheme of work found for a
  course - fine for now, would need refinement if a course accumulates multiple schemes.
- Generated fallback images are simple shapes, not illustrations - set an Unsplash key or
  build up the Media Library for real photos.
- No visual template/branding support yet since you don't have an existing PowerPoint
  template to build against - the color palette is chosen automatically per lesson topic.

**New environment variable (optional):**
```
UNSPLASH_ACCESS_KEY=your-free-unsplash-api-key
```
Leave blank to skip real photos entirely (teacher library + generated fallback still work).

**New database tables** (`lesson_decks`, `lesson_images`) - no reset needed, same as the last
couple of updates (new tables only, not changed columns).

---

## Parsing reliability fix - bounded, cancellable, and diagnosable (latest update)

This addresses "parsing takes hours / I can't tell if it's even running":

1. **Hard AI call budget (`MAX_AI_CALLS_PER_PARSE`, default 15).** Parsing a spec now
   NEVER runs unbounded. It either finishes via rules/learned patterns quickly, or stops
   cleanly after a fixed number of AI calls and tells you plainly what's left - verified
   with a test forcing an unparseable document through a tiny budget: it stopped exactly
   at the limit instead of continuing.
2. **A real Cancel button + a live "last updated" timestamp** on the spec page while
   parsing. You can now always tell whether it's actually progressing (timestamp moving)
   or genuinely stuck, and bail out immediately either way - verified with a test that
   triggers cancellation mid-run and confirms it stops within a couple of calls, not the
   full budget.
3. **`SPEC_PARSING_LLM_PROVIDER` (optional).** Set this to `anthropic` in `.env` to route
   just spec parsing's AI calls through the hosted model, even while keeping everyday
   marking on free/local Ollama. Parsing is a rare, one-off action per document, so cost is
   a non-issue here - and it removes the local CPU/memory bottleneck that's the likely real
   cause of multi-hour parses.
4. **Ollama's memory cap raised from 6GB to 10GB** in `docker-compose.yml` - 6GB was
   likely too tight for an 8B model under sustained load, risking stalls or crash-restart
   cycles that look exactly like "hours" or "not loading." If you still see this, also
   check Docker Desktop's own VM memory allocation (Settings → Resources → Memory) - a
   low cap there starves everything regardless of the per-container limit.

**Database reset needed for this one:** the two new fields (`parse_progress_updated_at`,
`cancel_requested`) are new COLUMNS on the existing `spec_versions` table, not a new table -
SQLAlchemy's auto-create only adds missing tables, not missing columns on ones that already
exist. Run `rm -f data/tigarsec_assess.db` before starting this version (you'll need to
re-register and re-upload/re-attach specs, same as the v1→v2 jump).

If parsing is still slow after this update, the honest read is: your machine's CPU/RAM is
the bottleneck for local inference on documents that need AI at all. The bounded budget and
cancel button mean that's no longer able to trap you for hours - but genuine speed on
AI-requiring documents means either a learned pattern (automatic after the first document of
a format) or `SPEC_PARSING_LLM_PROVIDER=anthropic`.

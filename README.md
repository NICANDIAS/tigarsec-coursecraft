# TIGARSEC CourseCraft

An AI-assisted marking, lesson-planning, and PowerPoint tool for UK further-education
lecturers. Self-hosted, works with either a free local model (Ollama) or a hosted model
(Anthropic's Claude), and is built around one rule throughout: the AI proposes, the
teacher decides. Nothing gets released to a student, or trusted as a finished document,
without your review.

---

## Running it

```bash
cd tigarsec-assess-v2
docker compose up --build
```

Open **http://localhost:8000**. The first account you register becomes the protected
admin (can't be demoted or deactivated, even by another admin).

Database schema changes are handled automatically by Alembic on every startup - you do
not need to reset the database when pulling in an update, unless a specific update
explicitly says otherwise.

---

## Configuring the AI provider

Edit `.env`:

```
LLM_PROVIDER=ollama        # or anthropic
```

- **`ollama`** (default) - a model running locally via Ollama, bundled in
  `docker-compose.yml`, no account or API key needed. Completely free. On CPU-only
  hardware, expect this to be noticeably slower and less reliable on complex tasks
  (scheme of work generation, PowerPoint generation, marking against a large spec) than a
  hosted model - if you see garbled or empty AI responses, check
  `docker compose exec app env | grep LLM_PROVIDER` first; every failure now also states
  which provider handled that specific call, e.g. `[Provider used: ollama]`, so you never
  have to guess.
- **`anthropic`** - requires `ANTHROPIC_API_KEY` in `.env`. Pay-as-you-go, no minimum -
  typical costs are a few pence per marking/generation action. Significantly more
  reliable for anything beyond simple spec parsing.

You can also route **just spec parsing** through a different provider than everyday
marking/generation, via `SPEC_PARSING_LLM_PROVIDER=anthropic` - useful since parsing a
spec is a rare, one-off action, so it's a low-cost place to use the more reliable model
even if you're keeping day-to-day marking on free/local Ollama.

`UNSPLASH_ACCESS_KEY` (optional) enables real stock photos on generated lesson slides
instead of placeholder graphics - free tier at unsplash.com/developers. Test your key
directly from Media Library → "Test Unsplash connection" rather than guessing why photos
aren't appearing. Use the **Access Key** specifically, not the Secret Key - that's the
most common cause of it silently not working.

---

## What's in here

### Spec library
Upload a specification once (PDF/docx/text) - admins and curriculum managers only, so
teachers attach an existing spec to their course from a shared list rather than each
re-uploading the same document. Parsing is hybrid: hardcoded rules handle BTEC-style
(Unit/Learning aim/P-M-D), T-Level-style (Content area/decimal codes), and Gateway
Qualifications-style (Unit Number/decimal criteria) specs instantly with zero AI cost;
anything else falls back to AI parsing, which also *learns* new formats so the next spec
of that shape parses instantly too. A hard call budget means parsing a document can never
run for hours unbounded - it either finishes fast or stops cleanly with a clear reason.
Always spot-check a freshly parsed spec before relying on it, the same way you'd sanity-
check any automated extraction of an official document.

### Scheme of work
Generate a week-by-week teaching plan from a spec's selected units/learning outcomes.
- Pick specific units and learning outcomes, not necessarily the whole spec at once.
- Optionally specify fixed **assessment weeks** in advance (e.g. "Week 7: Set Task A") -
  the plan gets paced around them as fixed calendar commitments, not vague suggestions.
  Leave them blank and the AI decides placement freely - both work fine.
- Optionally upload your college's own SOW format/template (docx tables are read
  correctly, not just paragraphs) - used as a structural/style guide. If the uploaded
  sample already contains real filled-in content rather than a blank template, that's
  detected automatically and the AI is told explicitly not to reuse its topics/wording.
- No fixed week-count cap - very large content areas are handled by the AI prioritising
  and grouping content sensibly, or explicitly telling you it needs more/fewer weeks than
  requested, never by silently cramming or returning nothing.
- **Download as a Word document** (a general table layout, not a copy of any specific
  college's exact template) alongside the in-app view.
- Each week can **generate a PowerPoint directly**, pre-filling the right content area,
  learning outcomes, and even the previous week's summary as retrieval-starter context.
  Once generated, a link back to that PowerPoint appears right on the week - you don't
  have to hunt through Tasks to find it again.

### Lesson PowerPoints
A fixed pedagogical structure per lesson: retrieval starter (last lesson, not today's) →
objectives → student-led content (guiding questions, not a lecture dump) → real-life
examples → timed activities/practicals → retrieval plenary → objectives review with
self-assessment prompts.
- **Duration self-assessment**: the AI honestly judges whether the selected content fits
  the requested lesson length (practical ceiling ~3 hours) - if not, it scopes the lesson
  to a coherent subset, tells you clearly what's covered vs what needs a follow-up
  session, and offers a **"Generate follow-up lesson"** button that carries the same
  unit/LOs and picks up where the first lesson left off.
- **Activity resources**: if an activity/practical needs something printed to actually run
  it (a sample data table, cards to cut up, a scenario brief), the actual content is
  generated, not just described - downloadable as a separate Word document, and
  re-downloadable any time afterwards without regenerating the lesson.
- **Concrete practical steps**: practicals involving a specific tool get the actual
  commands/steps to run in sequence, not a vague description.
- **Present in browser**: a full-screen, keyboard-navigable web view built from the exact
  same content, images, and colour palette as the PowerPoint - no PowerPoint installation
  or download needed to actually teach the lesson. The real `.pptx` file is still the
  primary output and opens directly in PowerPoint/Keynote/Google Slides for any editing
  beyond what this tool offers.
- Images resolve through three tiers, always falling back gracefully: a real photo via
  Unsplash → a tagged image from the shared Media Library → a themed placeholder graphic.

### Marking
Upload one or more files per submission (a report, a presentation, a spreadsheet, a PDF -
whatever evidences the criteria) - marking considers all of them together.
- Feedback cites exactly which file and location the evidence came from (slide number,
  page number, sheet/row, question number where detectable).
- Marks strictly against the spec's own command words (explain/describe/justify/analyse/
  etc.) - a criterion isn't met just because the topic was mentioned, and the exact
  command word driving the judgement is bolded in the feedback so you can see why.
- Supports "Partly met" and custom rubrics/numeric scales (upload or describe the scale
  per submission) alongside the default Met/Partly met/Not met.
- **Overall grade** (Pass/Merit/Distinction) is computed using the standard rule: a band
  is only achieved if every criterion in it, and every band below it, is met - one unmet
  Merit criterion blocks Merit and Distinction even if every other criterion is met.
- Flags criteria for **human review** with a specific reason when the AI genuinely can't
  assess something itself (most commonly when the actual evidence is an image/diagram
  that didn't extract as text) - never guesses or invents evidence.
- **Similarity check** runs automatically on every upload, comparing against other
  students' submissions for the same assignment - a warning appears immediately if a
  match is found. A manual "Run similarity check" page also exists for bulk re-scanning
  (e.g. older submissions uploaded before this existed). This is a fast, free text-
  similarity check on your own class's work, not a full Turnitin-style check against the
  wider internet/academic archive.

### Curriculum coverage
Shows which spec criteria have never been taught (per the scheme of work), taught but
never assessed, or where students are collectively struggling (more not-met than met
despite being assessed) - built entirely from data already in the system, no AI cost.
Coverage is tracked at the learning-outcome level, matching the level of detail a scheme
of work actually plans at, not forced down to individual P/M/D criteria.

### Internal Verification (IV)
Send an already-marked submission to a colleague for IV (the AI prepares a first-pass
suggestion per criterion to speed up their review), or upload work that was marked
entirely outside this tool for AI-assisted IV.

### Tasks
A single place to see everything currently generating (specs parsing, submissions
marking, schemes of work and PowerPoints generating) and what finished recently, with
timestamps - so nothing you started earlier gets lost as you navigate elsewhere. The
sidebar badge updates live.

### Admin
- User management (create accounts, reset passwords, change roles, deactivate).
- **Audit log** - who did what and when (uploads, approvals, course deletions), for record
  and accountability purposes.
- **Force-delete a course** - destroys a course and everything under it (students,
  submissions, marks, override history) regardless of existing work, requiring the exact
  course name typed as confirmation. Worth genuine caution: assessment records often need
  retaining for moderation/appeals/audit purposes for a period after the fact, and this
  has no way back. The regular "Delete course" option (available to any teacher) is
  blocked automatically if the course has any students, scheme of work, or PowerPoints -
  that's the safer default for routine cleanup.
- Configurable feedback-comment styles (Settings) - add a category beyond the built-in
  "standard" and "What Went Well / Even Better If", no code change needed.

---

## Known limitations

- **Analytics recalculates on every visit** rather than being pre-aggregated - fine at
  current scale, worth revisiting if usage grows significantly.
- **No true corpus-wide plagiarism check** - the similarity check compares only within
  your own class's submissions for the same assignment, not against the internet or
  other institutions' archives.
- **PowerPoint export uses a fixed general template shape** for the scheme-of-work Word
  document - it doesn't reshape itself to match an arbitrary uploaded college template's
  exact columns/branding.
- **Present mode is a functional web view, not a pixel-perfect match** of the actual
  `.pptx` styling - the content, images, and colour palette are identical, but fine
  layout details differ from what PowerPoint itself renders.
- **Teams/Graph integration and Timetabling** are not built - sidebar placeholders only.
- **Packet Tracer submissions** are reviewed manually - no automated marking for that
  format yet.
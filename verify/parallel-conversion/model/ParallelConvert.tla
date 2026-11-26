--------------------------- MODULE ParallelConvert ---------------------------
(***************************************************************************)
(* One `reflow convert --jobs N` run (convert.convert with a process pool), *)
(* checked against what `--jobs 1` makes of the same document.              *)
(*                                                                         *)
(* Every action names the code it stands for. The pooled run is the state  *)
(* machine below; the serial run (no pool, no prefetch threads, page       *)
(* images in publish) is the operator Serial, computed with the same       *)
(* PageReader rules. See ../README.md for the coverage record.              *)
(*                                                                         *)
(* Abstractions (each is a statement about the code, checked by reading):  *)
(*  - A chunk, a planning chunk and an image batch are one page each       *)
(*    (chunk_ranges / _batches at this bound). What a page holds is not    *)
(*    modelled, only which page it is and in what order it is collected.   *)
(*  - The pool runs any submitted task at any time, in any order (a        *)
(*    superset of a ProcessPoolExecutor's schedules).                      *)
(*  - The parent's own reads of the PageReader (seed, read_links,          *)
(*    read_tags) are one step each; that is sound only while              *)
(*    ReaderOneCaller holds, which is checked.                             *)
(*  - A page whose pdfplumber parse raises (`bad`) raises in every mode    *)
(*    and on every attempt; the failure is in the PDF, not in the timing.  *)
(*  - A page the table finder parsed (`seeded`) parsed without error.      *)
(*  - A saved model (`saved`, reflow.saved) is one a run that published    *)
(*    made: its key holds every input of the model, so it is Serial's      *)
(*    model. Restoring it runs no pool task and starts no thread.          *)
(*  - A restored run may carry the page images (`carried`): the previous   *)
(*    output's pages/ is cloned into the stage in the parent, only when    *)
(*    its render key is this run's and it holds exactly the files this     *)
(*    run would write. They are then the render's, so the run has every    *)
(*    image and starts none (saved.Reuse._carry_pages, a pure copy).       *)
(*  - The text witness (ingest.text_trust.preflight in prepare) submits    *)
(*    one task per suspect page (`suspect`) and joins them all, in page    *)
(*    order, before prepare returns. A task that fails is recorded on its  *)
(*    page, never raised, so it is modelled as one that finished.          *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS
    N,      \* pages in the document
    Break   \* "none" for the code as it is; otherwise a deliberately broken
            \* variant, used only by `run.sh selftest` to show each property can fail

Pages == 1..N
Ord == [i \in Pages |-> i]          \* every page once, in page order: what -j 1 collects
Range(s) == {s[k] : k \in DOMAIN s}

Breaks == {"none", "extAsCompleted", "noFinish", "noCancelWait", "noAbandon",
           "noImageFinish", "triedFirst", "restoreNoImages", "witAsCompleted", "noWitJoin"}

\* The pages of S in ascending order: what read_pages gathers.
Sorted(S) == [i \in 1..Cardinality(S) |->
                CHOOSE n \in S : Cardinality({m \in S : m < n}) = i - 1]
ASSUME N \in Nat /\ N >= 1 /\ Break \in Breaks

VARIABLES
    \* The document, chosen once by Init and never changed.
    seeded,     \* pages engine._hand_over_parse seeds into the PageReader (no words)
    bad,        \* pages whose pdfplumber parse raises (read_pages)
    linkPages,  \* pages with link annotations (scan_links)
    tagPages,   \* pages with content under the tag tree (read_tags: `present`)
    tagged,     \* walked.top is non-empty
    overCap,    \* more than tags.MAX_TAG_PAGES pages: no tag read in either mode
    pooledPlan, \* at least pageplan.MIN_PAGES pages: planning goes to the pool
    scanRaises, \* scan_links raises (it does so in either mode)
    walkRaises, \* walk_tags raises something other than PdfError
    failAt,     \* "none", or the stage that raises: an extract chunk, a planning
                \* chunk, or a later pass (structure.finish .. review.queue)
    sharedPool, \* the pool is convert_many's (Workers given an executor): no
                \* shutdown at the end of this conversion
    saved,      \* a saved model with this conversion's key was proved (saved.Reuse.restore)
    carried,    \* ... and its page images are the previous output's, cloned into the stage
    suspect,    \* pages text_trust.judge_page calls doubt or gibberish: each is witnessed
    \* convert.convert, the parent thread.
    pc,         \* the step it is at
    raised,     \* an exception is propagating
    \* ingest.witness.read_pages: one future per suspect page; pages gathered, in order.
    wit, witOrder,
    \* parallel.extract_document and pageplan.plan_pages.
    ext, extOrder,      \* chunk futures; chunks collected, in collection order
    plan, planOrder,    \* planning futures; chunks collected
    \* render.pages.PageImages.
    img,        \* batch futures: idle | queued | running | done | cancelled
    imgWritten, \* pages whose image files are in the stage
    \* run.state keys.
    imagesIn,   \* "page_images"
    earlyIn,    \* prefetch.EARLY
    parseIn,    \* prefetch.PARSE
    walked,     \* "walked" present
    scan,       \* "links_scan" present
    \* prefetch._Early.
    eState, eWalked, eScan,
    \* prefetch._Parse.
    pPc, pMiss,
    \* pagecontent.PageReader: keys of _pages, those parsed with words, _tried.
    readP, wordP, tried,
    \* What evidence.witness got.
    links, tags, unchecked,
    \* output.staged: staging | removed | published
    stage

docVars    == <<seeded, bad, linkPages, tagPages, tagged, overCap, pooledPlan,
                scanRaises, walkRaises, failAt, sharedPool, saved, carried, suspect>>
mainVars   == <<pc, raised>>
witVars    == <<wit, witOrder>>
extVars    == <<ext, extOrder>>
planVars   == <<plan, planOrder>>
imgVars    == <<img, imgWritten, imagesIn>>
cacheVars  == <<earlyIn, parseIn, walked, scan>>
eVars      == <<eState, eWalked, eScan>>
pVars      == <<pPc, pMiss>>
readerVars == <<readP, wordP, tried>>
evVars     == <<links, tags, unchecked>>
vars == <<docVars, mainVars, witVars, extVars, planVars, imgVars, cacheVars, eVars, pVars,
          readerVars, evVars, stage>>

(* PageReader.get(S, W): the pages of S it parses, with the words of the pages in
   W (a subset of S). A page n is parsed when (n, n in W) is not in _tried and the
   page is not kept, or is kept without the words asked for. get adds them to
   _tried only AFTER they parsed (pagecontent.py, get): a parse that raises leaves
   them untried, so the next get parses them again. *)
MissingIn(S, W, rp, wp, tr) ==
    {n \in S : <<n, n \in W>> \notin tr /\ (n \notin rp \/ (n \in W /\ n \notin wp))}
Missing(S, W) == MissingIn(S, W, readP, wordP, tried)

\* The new _tried after a get of the pages `m` (asked with the words of W) that parsed (`ok`)
\* or raised. Break triedFirst: get marks them before it parses, so a raise leaves them tried.
TriedAfter(m, W, ok) ==
    IF ok \/ Break = "triedFirst" THEN tried \cup {<<n, n \in W>> : n \in m} ELSE tried

Wanted == IF tagged THEN tagPages ELSE {}      \* read_tags: `present`

-----------------------------------------------------------------------------
(* The serial run, convert(..., jobs=1): no pool, so no prefetch threads; the
   reader is seeded, then read_links reads the link pages with words, then
   shared_tags reads the tagged pages. Any raise before publish means no
   output. *)
Failed == [published |-> FALSE, ext |-> <<>>, plan |-> <<>>, links |-> {},
           tags |-> {}, unchecked |-> FALSE, images |-> {}, wit |-> <<>>]

Serial ==
    LET linkM    == MissingIn(linkPages, linkPages, seeded, {}, {})
        linkFail == scanRaises \/ (linkPages # {} /\ linkM \cap bad # {})
        rp       == seeded \cup linkM
        wp       == linkM
        tr       == {<<n, TRUE>> : n \in linkM}
        tagM     == MissingIn(Wanted, {}, rp, wp, tr)
        tagFail  == ~overCap /\ (walkRaises \/ tagM \cap bad # {})
    IN IF failAt # "none" \/ linkFail THEN Failed
       ELSE [published |-> TRUE, ext |-> Ord, plan |-> Ord,
             links |-> linkPages \cap wp,
             tags |-> IF overCap \/ tagFail THEN {} ELSE Wanted \cap (rp \cup tagM),
             unchecked |-> tagFail, images |-> Pages, wit |-> Sorted(suspect)]

Outcome ==
    IF pc = "published"
    THEN [published |-> TRUE, ext |-> extOrder, plan |-> planOrder, links |-> links,
          tags |-> tags, unchecked |-> unchecked, images |-> imgWritten, wit |-> witOrder]
    ELSE Failed

-----------------------------------------------------------------------------
Init ==
    /\ seeded \in SUBSET Pages
    /\ bad \in SUBSET Pages
    /\ bad \cap seeded = {}
    /\ linkPages \in SUBSET Pages
    /\ tagPages \in SUBSET Pages
    /\ tagged \in BOOLEAN
    /\ tagPages # {} => tagged
    /\ overCap \in BOOLEAN
    /\ pooledPlan \in BOOLEAN
    /\ scanRaises \in BOOLEAN
    /\ walkRaises \in BOOLEAN
    /\ failAt \in {"none", "extract", "plan", "pass"}
    /\ sharedPool \in BOOLEAN
    /\ suspect \in SUBSET Pages
    /\ saved \in BOOLEAN
    /\ saved => Serial.published     \* only a run that published keeps its model
    /\ carried \in BOOLEAN
    /\ carried => saved              \* only a restored run carries images (previous output)
    /\ pc = "restore" /\ raised = FALSE
    /\ wit = [i \in Pages |-> "idle"] /\ witOrder = <<>>
    /\ ext = [i \in Pages |-> "idle"] /\ extOrder = <<>>
    /\ plan = [i \in Pages |-> "idle"] /\ planOrder = <<>>
    /\ img = [b \in Pages |-> "idle"] /\ imgWritten = {} /\ imagesIn = FALSE
    /\ earlyIn = FALSE /\ parseIn = FALSE /\ walked = FALSE /\ scan = FALSE
    /\ eState = "idle" /\ eWalked = FALSE /\ eScan = FALSE
    /\ pPc = "idle" /\ pMiss = {}
    /\ readP = {} /\ wordP = {} /\ tried = {}
    /\ links = {} /\ tags = {} /\ unchecked = FALSE
    /\ stage = "staging"

Raise == pc' = "finally" /\ raised' = TRUE

(* prefetch._collect: join the early thread (block until it is done), then copy
   what it read into run.state. *)
CollectEarly ==
    /\ earlyIn => eState = "done"
    /\ earlyIn' = FALSE
    /\ walked' = (walked \/ (earlyIn /\ eWalked))
    /\ scan' = (scan \/ (earlyIn /\ eScan))

-----------------------------------------------------------------------------
(* The parent thread, in the order convert.convert runs it. *)

\* convert: saved.Reuse.restore copies a proved saved model into the stage (serial, in the
\* parent); the run then starts the page images and publishes. When it carried the previous
\* output's page images they are in the stage already (`carried`): it publishes with no image
\* task. Without a saved model, prepare and the passes run. Break restoreNoImages: the restored
\* run publishes without starting the images, carried or not.
Restore ==
    /\ pc = "restore"
    /\ IF saved
       THEN /\ extOrder' = Serial.ext /\ planOrder' = Serial.plan
            /\ links' = Serial.links /\ tags' = Serial.tags /\ unchecked' = Serial.unchecked
            /\ witOrder' = Serial.wit
            /\ pc' = IF carried \/ Break = "restoreNoImages" THEN "publish" ELSE "imgStart"
            /\ imgWritten' = IF carried THEN Pages ELSE imgWritten
       ELSE /\ pc' = "witSubmit" /\ UNCHANGED <<extOrder, planOrder, evVars, imgWritten, witOrder>>
    /\ UNCHANGED <<docVars, raised, wit, ext, plan, img, imagesIn, cacheVars, eVars, pVars,
                   readerVars, stage>>

\* prepare -> text_trust.preflight -> witness.read_pages: one future per suspect page.
WitSubmit ==
    /\ pc = "witSubmit"
    /\ wit' = [i \in Pages |-> IF i \in suspect THEN "queued" ELSE "idle"]
    /\ pc' = "witCollect"
    /\ UNCHANGED <<docVars, raised, witOrder, extVars, planVars, imgVars, cacheVars, eVars,
                   pVars, readerVars, evVars, stage>>

\* read_pages: `for n, future in zip(order, futures): future.result()`, in page order; an
\* error is the page's, never raised. Break witAsCompleted: whichever page is done first.
\* Break noWitJoin: prepare returns without waiting for the tasks.
WitCollect ==
    /\ pc = "witCollect"
    /\ IF Len(witOrder) = Cardinality(suspect) \/ Break = "noWitJoin"
       THEN /\ pc' = "early"
            /\ witOrder' = IF Break = "noWitJoin" THEN Sorted(suspect) ELSE witOrder
       ELSE /\ \E i \in suspect :
                 /\ IF Break = "witAsCompleted" THEN i \notin Range(witOrder)
                                                ELSE i = Sorted(suspect)[Len(witOrder) + 1]
                 /\ wit[i] = "done"
                 /\ witOrder' = Append(witOrder, i)
            /\ UNCHANGED pc
    /\ UNCHANGED <<docVars, raised, wit, extVars, planVars, imgVars, cacheVars, eVars, pVars,
                   readerVars, evVars, stage>>

\* analyze.engine._extract: prefetch.early(run) starts _Early on a thread.
Early ==
    /\ pc = "early"
    /\ earlyIn' = TRUE /\ eState' = "running"
    /\ pc' = "extSubmit"
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planVars, imgVars, parseIn, walked, scan,
                   eWalked, eScan, pVars, readerVars, evVars, stage>>

\* extract.parallel.extract_document: one future per chunk.
ExtSubmit ==
    /\ pc = "extSubmit"
    /\ ext' = [i \in Pages |-> "queued"]
    /\ pc' = "extCollect"
    /\ UNCHANGED <<docVars, witVars, raised, extOrder, planVars, imgVars, cacheVars, eVars, pVars,
                   readerVars, evVars, stage>>

\* extract_document: `for future in futures: future.result()`, in submission order.
\* Break extAsCompleted: collect whichever chunk is done first (as_completed).
ExtCollect ==
    /\ pc = "extCollect"
    /\ IF Len(extOrder) = N
       THEN pc' = "seed" /\ UNCHANGED <<raised, extOrder>>
       ELSE \E i \in Pages :
              /\ IF Break = "extAsCompleted" THEN i \notin Range(extOrder)
                                             ELSE i = Len(extOrder) + 1
              /\ ext[i] \in {"done", "failed"}
              /\ IF ext[i] = "failed"
                 THEN Raise /\ UNCHANGED extOrder
                 ELSE extOrder' = Append(extOrder, i) /\ UNCHANGED <<pc, raised>>
    /\ UNCHANGED <<docVars, witVars, ext, planVars, imgVars, cacheVars, eVars, pVars, readerVars,
                   evVars, stage>>

\* analyze.engine._hand_over_parse: reader_for(run).seed(the table finder's pages).
Seed ==
    /\ pc = "seed"
    /\ readP' = readP \cup seeded
    /\ pc' = IF pooledPlan THEN "planSubmit" ELSE "planSerial"
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planVars, imgVars, cacheVars, eVars, pVars,
                   wordP, tried, evVars, stage>>

\* analyze.pageplan.plan_pages: one future per chunk.
PlanSubmit ==
    /\ pc = "planSubmit"
    /\ plan' = [i \in Pages |-> "queued"]
    /\ pc' = "planStarted"
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planOrder, imgVars, cacheVars, eVars, pVars,
                   readerVars, evVars, stage>>

\* analyze.prefetch.start: join _Early, then start _Parse when there are links
\* or a tag tree to read (and _Early read both the tree and the links).
StartParse(next) ==
    /\ IF parseIn
       THEN UNCHANGED <<cacheVars, pPc>>
       ELSE /\ CollectEarly
            /\ IF walked' /\ scan' /\ (linkPages # {} \/ tagged)
               THEN parseIn' = TRUE /\ pPc' = "pGet"
               ELSE UNCHANGED <<parseIn, pPc>>
    /\ pc' = next
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planVars, imgVars, eVars, pMiss, readerVars,
                   evVars, stage>>

\* plan_pages: submitted() is analyze's on_planning, which is prefetch.start.
PlanStarted == pc = "planStarted" /\ StartParse("planCollect")

\* plan_pages: `for (a, b), future in zip(ranges, futures)`, in submission order.
PlanCollect ==
    /\ pc = "planCollect"
    /\ IF Len(planOrder) = N
       THEN pc' = "startAfter" /\ UNCHANGED <<raised, planOrder>>
       ELSE LET i == Len(planOrder) + 1 IN
              /\ plan[i] \in {"done", "failed"}
              /\ IF plan[i] = "failed"
                 THEN Raise /\ UNCHANGED planOrder
                 ELSE planOrder' = Append(planOrder, i) /\ UNCHANGED <<pc, raised>>
    /\ UNCHANGED <<docVars, witVars, extVars, plan, imgVars, cacheVars, eVars, pVars, readerVars,
                   evVars, stage>>

\* layout.analyze with fewer than MIN_PAGES pages: _plan_page in this process.
PlanSerial ==
    /\ pc = "planSerial"
    /\ IF failAt = "plan"
       THEN Raise /\ UNCHANGED planOrder
       ELSE planOrder' = Ord /\ pc' = "startAfter" /\ UNCHANGED raised
    /\ UNCHANGED <<docVars, witVars, extVars, plan, imgVars, cacheVars, eVars, pVars, readerVars,
                   evVars, stage>>

\* engine._heuristic: prefetch.start(run) after analyze (a no-op once _Parse started).
StartAfter == pc = "startAfter" /\ StartParse("imgStart")

\* output.start_page_images -> render.pages.start_page_images: one future per batch.
ImgStart ==
    /\ pc = "imgStart"
    /\ img' = [b \in Pages |-> "queued"]
    /\ imagesIn' = TRUE
    /\ pc' = IF saved THEN "publish" ELSE "finish"   \* restored: nothing else to run
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planVars, imgWritten, cacheVars, eVars, pVars,
                   readerVars, evVars, stage>>

\* prefetch.finish: _collect, then join _Parse.  Break noFinish: convert skips it.
Finish ==
    /\ pc = "finish"
    /\ IF Break = "noFinish"
       THEN UNCHANGED cacheVars
       ELSE /\ CollectEarly
            /\ IF parseIn THEN pPc = "done" /\ parseIn' = FALSE ELSE UNCHANGED parseIn
    /\ pc' = "links"
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planVars, imgVars, eVars, pVars, readerVars,
                   evVars, stage>>

\* evidence.witness -> links.read_links: scan_links again if
\* the early read did not leave it, then reader.get(sorted(pending), words=True).
\* Not guarded: a raise here fails the conversion.
Links ==
    /\ pc = "links"
    /\ IF ~scan /\ scanRaises
       THEN Raise /\ UNCHANGED <<readerVars, links>>
       ELSE IF linkPages = {}
       THEN pc' = "tags" /\ UNCHANGED <<raised, readerVars, links>>
       ELSE LET m == Missing(linkPages, linkPages) IN
              /\ tried' = TriedAfter(m, linkPages, m \cap bad = {})
              /\ IF m \cap bad # {}
                 THEN Raise /\ UNCHANGED <<readP, wordP, links>>
                 ELSE /\ readP' = readP \cup m
                      /\ wordP' = wordP \cup m
                      /\ links' = linkPages \cap wordP'   \* pages read with words
                      /\ pc' = "tags" /\ UNCHANGED raised
    /\ UNCHANGED <<docVars, witVars, extVars, planVars, imgVars, cacheVars, eVars, pVars, tags,
                   unchecked, stage>>

\* evidence.witness -> tags.shared_tags -> read_tags: reader.get(present). Guarded:
\* a raise leaves the tags empty and an "order-unchecked" fact.
Tags ==
    /\ pc = "tags"
    /\ IF overCap
       THEN UNCHANGED <<readerVars, tags, unchecked>>
       ELSE IF ~walked /\ walkRaises
       THEN unchecked' = TRUE /\ UNCHANGED <<readerVars, tags>>
       ELSE LET m == Missing(Wanted, {}) IN
              /\ tried' = TriedAfter(m, {}, m \cap bad = {})
              /\ IF m \cap bad # {}
                 THEN unchecked' = TRUE /\ UNCHANGED <<readP, wordP, tags>>
                 ELSE /\ readP' = readP \cup m
                      /\ tags' = Wanted \cap readP'
                      /\ UNCHANGED <<wordP, unchecked>>
    /\ pc' = "passes"
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planVars, imgVars, cacheVars, eVars, pVars,
                   links, stage>>

\* order_evidence.apply .. review.queue: any of them may raise.
Passes ==
    /\ pc = "passes"
    /\ IF failAt = "pass" THEN Raise ELSE pc' = "publish" /\ UNCHANGED raised
    /\ UNCHANGED <<docVars, witVars, extVars, planVars, imgVars, cacheVars, eVars, pVars, readerVars,
                   evVars, stage>>

\* output.publish: run.state.page_images.finish(...) waits for every batch.
\* Break noImageFinish: pops them without waiting.
Publish ==
    /\ pc = "publish"
    /\ imagesIn /\ Break # "noImageFinish" => \A b \in Pages : img[b] = "done"
    /\ imagesIn' = FALSE
    /\ pc' = "finally"
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planVars, img, imgWritten, cacheVars, eVars,
                   pVars, readerVars, evVars, stage>>

\* convert's `finally`: PageImages.cancel() for images still in run.state:
\* future.cancel() on each (only a queued one is cancelled) ...
Finally ==
    /\ pc = "finally"
    /\ IF imagesIn
       THEN /\ img' = [b \in Pages |-> IF img[b] = "queued" THEN "cancelled" ELSE img[b]]
            /\ imagesIn' = FALSE
            /\ pc' = "cancelWait"
       ELSE pc' = "abandon" /\ UNCHANGED <<img, imagesIn>>
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planVars, imgWritten, cacheVars, eVars, pVars,
                   readerVars, evVars, stage>>

\* ... then wait(self.futures) for the ones already running.
\* Break noCancelWait: cancel() without the wait.
CancelWait ==
    /\ pc = "cancelWait"
    /\ Break # "noCancelWait" => \A b \in Pages : img[b] # "running"
    /\ pc' = "abandon"
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planVars, imgVars, cacheVars, eVars, pVars,
                   readerVars, evVars, stage>>

\* prefetch.abandon: join the threads still in run.state.  Break noAbandon: skip it.
Abandon ==
    /\ pc = "abandon"
    /\ Break # "noAbandon" => (earlyIn => eState = "done") /\ (parseIn => pPc = "done")
    /\ earlyIn' = FALSE /\ parseIn' = FALSE
    /\ pc' = "shutdown"
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planVars, imgVars, walked, scan, eVars, pVars,
                   readerVars, evVars, stage>>

\* run.workers_for: a private pool is shut down (cancel_futures=True, wait=True)
\* before output.staged acts; convert_many's shared pool is left running.
Shutdown ==
    /\ pc = "shutdown"
    /\ IF sharedPool
       THEN UNCHANGED img
       ELSE /\ \A b \in Pages : img[b] # "running"
            /\ img' = [b \in Pages |-> IF img[b] = "queued" THEN "cancelled" ELSE img[b]]
    /\ pc' = IF raised THEN "rmtree" ELSE "swap"
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planVars, imgWritten, imagesIn, cacheVars,
                   eVars, pVars, readerVars, evVars, stage>>

\* output.staged: rmtree(stage) on an exception ...
Rmtree ==
    /\ pc = "rmtree"
    /\ stage' = "removed" /\ pc' = "failed"
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planVars, imgVars, cacheVars, eVars, pVars,
                   readerVars, evVars>>

\* ... or _swap(stage, out_dir) on a clean exit.
Swap ==
    /\ pc = "swap"
    /\ stage' = "published" /\ pc' = "published"
    /\ UNCHANGED <<docVars, witVars, raised, extVars, planVars, imgVars, cacheVars, eVars, pVars,
                   readerVars, evVars>>

-----------------------------------------------------------------------------
(* The other threads and the pool. *)

\* prefetch._Early.run: walk_tags (if not over the cap), then scan_links; an
\* exception ends the thread with what it had.
EarlyRun ==
    /\ eState = "running"
    /\ eState' = "done"
    /\ eWalked' = (~overCap /\ ~walkRaises)
    /\ eScan' = (~(~overCap /\ walkRaises) /\ ~scanRaises)
    /\ UNCHANGED <<docVars, witVars, mainVars, extVars, planVars, imgVars, cacheVars, pVars,
                   readerVars, evVars, stage>>

\* prefetch._Parse.run: one reader.get of the link pages and the pages the tag tree has
\* content on (`Wanted`), with the words of the link pages. It parses the pages on the pool,
\* then keeps them and marks them tried; an exception ends the thread (swallowed) with
\* the pages untried.
PGetBegin(S, W, readingPc, skipPc) ==
    LET m == Missing(S, W) IN
    IF m = {}
    THEN pPc' = skipPc /\ UNCHANGED <<pMiss, readerVars>>
    ELSE /\ pMiss' = m
         /\ pPc' = readingPc
         /\ UNCHANGED <<readP, wordP, tried>>

PGetEnd(W, nextPc) ==
    /\ tried' = TriedAfter(pMiss, W, pMiss \cap bad = {})
    /\ IF pMiss \cap bad # {}
       THEN pPc' = "done" /\ UNCHANGED <<readP, wordP>>
       ELSE /\ readP' = readP \cup pMiss
            /\ wordP' = wordP \cup (pMiss \cap W)
            /\ pPc' = nextPc
    /\ pMiss' = {}

PGet ==
    /\ pPc = "pGet"
    /\ PGetBegin(linkPages \cup Wanted, linkPages, "pGetRead", "done")
    /\ UNCHANGED <<docVars, witVars, mainVars, extVars, planVars, imgVars, cacheVars, eVars, evVars,
                   stage>>

PGetRead ==
    /\ pPc = "pGetRead"
    /\ PGetEnd(linkPages, "done")
    /\ UNCHANGED <<docVars, witVars, mainVars, extVars, planVars, imgVars, cacheVars, eVars, evVars,
                   stage>>

\* A pool worker finishes an extraction or planning chunk (any order).
ExtTask(i) ==
    /\ ext[i] = "queued"
    /\ ext' = [ext EXCEPT ![i] = IF failAt = "extract" /\ i = N THEN "failed" ELSE "done"]
    /\ UNCHANGED <<docVars, witVars, mainVars, extOrder, planVars, imgVars, cacheVars, eVars, pVars,
                   readerVars, evVars, stage>>

PlanTask(i) ==
    /\ plan[i] = "queued"
    /\ plan' = [plan EXCEPT ![i] = IF failAt = "plan" /\ i = N THEN "failed" ELSE "done"]
    /\ UNCHANGED <<docVars, witVars, mainVars, extVars, planOrder, imgVars, cacheVars, eVars, pVars,
                   readerVars, evVars, stage>>

\* A pool worker starts, then finishes, a witness page (it reads working.pdf in the stage).
WitRun(i) ==
    /\ wit[i] = "queued"
    /\ wit' = [wit EXCEPT ![i] = "running"]
    /\ UNCHANGED <<docVars, mainVars, witOrder, extVars, planVars, imgVars, cacheVars, eVars,
                   pVars, readerVars, evVars, stage>>

WitDone(i) ==
    /\ wit[i] = "running"
    /\ wit' = [wit EXCEPT ![i] = "done"]
    /\ UNCHANGED <<docVars, mainVars, witOrder, extVars, planVars, imgVars, cacheVars, eVars,
                   pVars, readerVars, evVars, stage>>

\* A pool worker starts, then finishes, a page-image batch (_render_batch writes
\* the files into the stage's pages/ folder).
ImgRun(b) ==
    /\ img[b] = "queued"
    /\ img' = [img EXCEPT ![b] = "running"]
    /\ UNCHANGED <<docVars, witVars, mainVars, extVars, planVars, imgWritten, imagesIn, cacheVars,
                   eVars, pVars, readerVars, evVars, stage>>

ImgDone(b) ==
    /\ img[b] = "running"
    /\ img' = [img EXCEPT ![b] = "done"]
    /\ imgWritten' = imgWritten \cup {b}
    /\ UNCHANGED <<docVars, witVars, mainVars, extVars, planVars, imagesIn, cacheVars, eVars, pVars,
                   readerVars, evVars, stage>>

\* convert returned (or raised): nothing left for the parent to do.
Ended == pc \in {"published", "failed"} /\ UNCHANGED vars

Next ==
    \/ Restore \/ WitSubmit \/ WitCollect \/ Early \/ ExtSubmit \/ ExtCollect \/ Seed \/ PlanSubmit \/ PlanStarted
    \/ PlanCollect \/ PlanSerial \/ StartAfter \/ ImgStart \/ Finish \/ Links \/ Tags
    \/ Passes \/ Publish \/ Finally \/ CancelWait \/ Abandon \/ Shutdown \/ Rmtree \/ Swap
    \/ EarlyRun \/ PGet \/ PGetRead
    \/ \E i \in Pages : ExtTask(i) \/ PlanTask(i) \/ ImgRun(i) \/ ImgDone(i)
                       \/ WitRun(i) \/ WitDone(i)
    \/ Ended

Spec == Init /\ [][Next]_vars

-----------------------------------------------------------------------------
(* Properties. *)

PThreadAlive == pPc \notin {"idle", "done"}

\* SameAsSerial: a conversion with a pool publishes exactly what -j 1 publishes
\* (pages and plans in page order, the same links, tag evidence, order-unchecked
\* fact and page images), and fails exactly when -j 1 fails.
SameAsSerial == pc \in {"published", "failed"} => Outcome = Serial

\* The same, for documents whose every page parses.
SameAsSerialWhenPagesParse == bad = {} => SameAsSerial

Ended2 == pc \in {"published", "failed"}
BothPublish == Ended2 /\ Outcome.published /\ Serial.published

\* For every document: when both runs publish, the pages, the plans and the page
\* images are the serial run's, in page order.
SameOrderAndImages ==
    BothPublish => /\ Outcome.wit = Serial.wit
                   /\ Outcome.ext = Serial.ext
                   /\ Outcome.plan = Serial.plan
                   /\ Outcome.images = Serial.images

\* F1 (a finding until #78): the pooled run fails exactly when -j 1 fails, and keeps the
\* same links.
F1SameFailureAndLinks ==
    Ended2 => /\ Outcome.published = Serial.published
              /\ BothPublish => Outcome.links = Serial.links

\* F2 (a finding until #78): when both publish, the same tag evidence and the same
\* "order-unchecked" fact.
F2SameTagEvidence ==
    BothPublish => /\ Outcome.tags = Serial.tags
                   /\ Outcome.unchecked = Serial.unchecked

\* PageReader is not thread-safe: while _Parse is alive the parent never uses it
\* (prefetch.py's docstring).
ReaderOneCaller == ~(pc \in {"seed", "links", "tags"} /\ PThreadAlive)

\* Nothing reads or writes the staging folder after output.staged removed it.
StageQuiet ==
    stage = "removed" =>
        /\ \A b \in Pages : img[b] # "running" /\ wit[b] \notin {"queued", "running"}
        /\ eState # "running"
        /\ ~PThreadAlive

\* prepare returns only once every witness task is done: nothing reads working.pdf for the
\* witness while extraction and the later passes run, and the facts are all on the pages.
WitnessJoined == pc \notin {"restore", "witSubmit", "witCollect"} =>
                     \A i \in Pages : wit[i] \in {"idle", "done"}

\* A published output has every page image.
PublishedWhole == stage = "published" => imgWritten = Pages

TypeOK ==
    /\ pc \in {"restore", "witSubmit", "witCollect", "early", "extSubmit", "extCollect", "seed", "planSubmit", "planStarted",
               "planCollect", "planSerial", "startAfter", "imgStart", "finish", "links",
               "tags", "passes", "publish", "finally", "cancelWait", "abandon", "shutdown",
               "rmtree", "swap", "published", "failed"}
    /\ ext \in [Pages -> {"idle", "queued", "done", "failed"}]
    /\ plan \in [Pages -> {"idle", "queued", "done", "failed"}]
    /\ img \in [Pages -> {"idle", "queued", "running", "done", "cancelled"}]
    /\ wit \in [Pages -> {"idle", "queued", "running", "done"}]
    /\ eState \in {"idle", "running", "done"}
    /\ pPc \in {"idle", "pGet", "pGetRead", "done"}
    /\ readP \subseteq Pages /\ wordP \subseteq readP
    /\ stage \in {"staging", "removed", "published"}
=============================================================================

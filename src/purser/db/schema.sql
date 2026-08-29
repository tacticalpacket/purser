-- purser schema (DuckDB)
--
-- M1 Ledger. See docs/DESIGN.md.
--
-- Conventions that the rest of the system depends on:
--
--   Money is DECIMAL(18,2), never DOUBLE. Binary floats cannot represent
--   cents exactly and a reconciliation that is off by 0.00000001 is a
--   reconciliation that fails for no reason.
--
--   Signs are stored normalized, not as the source wrote them. `amount` is
--   negative when value leaves the account and positive when it arrives, for
--   every account type. Adapters are responsible for translating whatever
--   convention their institution uses into this one; see the NFCU note in
--   src/purser/ingest/nfcu_csv.py. Storing the source's convention and
--   "remembering to flip it later" is the classic silent defect in this
--   domain, so it is resolved once, at the boundary.
--
--   For a credit card (a liability) the same rule reads naturally: a purchase
--   is negative because value left, and the ledger balance is negative when
--   money is owed. That matches how the institution itself reports the
--   balance, so no per-type special case is needed downstream.

-- ---------------------------------------------------------------------------
-- Sequences. DuckDB has no AUTOINCREMENT.
-- ---------------------------------------------------------------------------
CREATE SEQUENCE IF NOT EXISTS seq_account_id     START 1;
CREATE SEQUENCE IF NOT EXISTS seq_import_id      START 1;
CREATE SEQUENCE IF NOT EXISTS seq_transaction_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_transfer_id    START 1;
CREATE SEQUENCE IF NOT EXISTS seq_merchant_id    START 1;
CREATE SEQUENCE IF NOT EXISTS seq_category_id    START 1;
CREATE SEQUENCE IF NOT EXISTS seq_balance_id     START 1;

-- ---------------------------------------------------------------------------
-- accounts: mirrors config/accounts.yaml, which is the source of truth.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts (
    account_id    INTEGER PRIMARY KEY DEFAULT nextval('seq_account_id'),
    alias         VARCHAR NOT NULL UNIQUE,
    institution   VARCHAR NOT NULL,
    account_type  VARCHAR NOT NULL
        CHECK (account_type IN ('checking','savings','credit_card',
                                'brokerage','ira','loan')),
    currency      VARCHAR NOT NULL DEFAULT 'USD',
    -- 'asset'     -> positive balance means the captain holds value
    -- 'liability' -> negative balance means the captain owes value
    balance_sign  VARCHAR NOT NULL DEFAULT 'asset'
        CHECK (balance_sign IN ('asset','liability')),
    adapter       VARCHAR,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- import_log: one row per import RUN, with enough provenance that any stored
-- transaction can be traced back to the exact bytes it came from.
--
-- "The exact file" is source_sha256, not source_path: paths get moved and
-- re-downloaded exports reuse names, but the digest identifies the content.
-- Re-importing an identical file is expected and safe (imports are
-- idempotent), so a repeated digest is recorded rather than rejected.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS import_log (
    import_id       INTEGER PRIMARY KEY DEFAULT nextval('seq_import_id'),
    account_id      INTEGER NOT NULL REFERENCES accounts(account_id),

    -- File identity
    source_path     VARCHAR NOT NULL,   -- as given, for a human to find again
    source_filename VARCHAR NOT NULL,
    source_sha256   VARCHAR NOT NULL,   -- authoritative file identity
    source_bytes    BIGINT  NOT NULL,
    source_mtime    TIMESTAMPTZ,

    -- Run identity
    adapter         VARCHAR NOT NULL,
    adapter_version VARCHAR NOT NULL,
    purser_version  VARCHAR NOT NULL,
    hostname        VARCHAR,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          VARCHAR NOT NULL DEFAULT 'running'
        CHECK (status IN ('running','ok','failed')),

    -- Outcome. rows_parsed = rows_inserted + rows_duplicate.
    rows_in_file    INTEGER NOT NULL DEFAULT 0,
    rows_parsed     INTEGER NOT NULL DEFAULT 0,
    rows_inserted   INTEGER NOT NULL DEFAULT 0,
    rows_duplicate  INTEGER NOT NULL DEFAULT 0,
    rows_rejected   INTEGER NOT NULL DEFAULT 0,

    -- Observed extent of the data, cheap to compute and useful for gap checks
    min_posted_date DATE,
    max_posted_date DATE,
    net_amount      DECIMAL(18,2),

    notes           VARCHAR
);

-- ---------------------------------------------------------------------------
-- merchants: normalized payee identities. Raw descriptions arrive padded with
-- terminal ids, store numbers and city/state noise; the normalized name is
-- what reports group by.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS merchants (
    merchant_id     INTEGER PRIMARY KEY DEFAULT nextval('seq_merchant_id'),
    canonical_name  VARCHAR NOT NULL UNIQUE,
    -- How this mapping was established. Corrections become rules, so a
    -- confirmed mapping outranks a guess.
    source          VARCHAR NOT NULL DEFAULT 'rule'
        CHECK (source IN ('rule','llm','manual')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- categories: the hierarchy from the audit plan. Self-referencing parent so
-- the depth is not baked into the schema.
--
-- Owned by config/categories.yaml and config/rules/, which a separate change
-- populates. This table is the shape those files load into.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS categories (
    category_id  INTEGER PRIMARY KEY DEFAULT nextval('seq_category_id'),
    name         VARCHAR NOT NULL,
    parent_id    INTEGER REFERENCES categories(category_id),
    -- Full dotted path, e.g. 'food.delivery'. Unique so rules can address a
    -- category by a stable name rather than a generated id.
    path         VARCHAR NOT NULL UNIQUE,
    is_essential BOOLEAN NOT NULL DEFAULT false,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Assignment authority: who may overwrite whom
--
-- Three different things assign a category, and they run at different times
-- and re-run independently: a deterministic rule out of
-- config/rules/categories.yaml, an LLM pass over whatever the rules did not
-- match, and the captain correcting one by hand. Without a stated precedence,
-- the next rules re-run silently overwrites a manual correction and the
-- correction is simply gone -- no error, no trace, just a wrong number in a
-- report months later.
--
-- The precedence, in force for both category and merchant assignment:
--
--     manual (3)   >   rule (2)   >   llm (1)
--
-- Read as: a pass may write an assignment only when its own authority is
-- GREATER THAN OR EQUAL TO the authority already on the row.
--
--   * Equal is allowed on purpose. A rules re-run must be able to refresh a
--     row it assigned itself, and a later manual correction must be able to
--     replace an earlier one.
--   * Strictly lower is refused. A rules re-run cannot touch a manual
--     override; an LLM pass cannot touch either.
--   * An unassigned row (category_source IS NULL) has no authority, so any
--     pass may claim it.
--
-- Enforced in src/purser/core/categorize.py, which is the only supported
-- writer of these columns. It is restated here because it is a property of
-- the data rather than of one module: anything that writes these columns by
-- hand and skips the check corrupts the ledger silently.
--
-- A refused assignment is RECORDED, not discarded. category_declined_* holds
-- the most recent losing proposal, so "the AI wanted to call this something
-- else" stays visible. Deliberately one slot and no review workflow.
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- transactions: the canonical ledger.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id   INTEGER PRIMARY KEY DEFAULT nextval('seq_transaction_id'),
    account_id       INTEGER NOT NULL REFERENCES accounts(account_id),

    -- Dates. posted_date is what the balance moves on and is what the balance
    -- check sums by; transaction_date is when it actually happened and is what
    -- spending analysis should group by. They differ on most rows.
    posted_date      DATE NOT NULL,
    transaction_date DATE,

    -- Normalized: negative = value left the account, positive = value arrived.
    amount           DECIMAL(18,2) NOT NULL,
    currency         VARCHAR NOT NULL DEFAULT 'USD',

    description      VARCHAR NOT NULL,   -- source text, whitespace-collapsed
    source_type      VARCHAR,            -- institution's own type, e.g. 'POS'
    source_category  VARCHAR,            -- institution's own guess, kept as a hint
    check_number     VARCHAR,
    card_ending      VARCHAR,

    -- Dedupe identity. See src/purser/core/fingerprint.py.
    --   fitid       institution-assigned id when the source supplies one.
    --               NFCU's CSV export leaves its Reference column empty on
    --               every row, so this is NULL for that adapter.
    --   fingerprint deterministic content hash, always present.
    --   dedupe_key  COALESCE(fitid, fingerprint) -- DESIGN.md's
    --               "FITID-plus-fingerprint": prefer the institution's id,
    --               fall back to content. Stored rather than generated
    --               because DuckDB cannot put a constraint on a generated
    --               column, and this column must carry the uniqueness.
    fitid            VARCHAR,
    fingerprint      VARCHAR NOT NULL,
    dedupe_key       VARCHAR NOT NULL,

    -- Enrichment, populated by later passes. See the assignment-authority
    -- block below: every enrichment column carries the source that wrote it.
    is_transfer      BOOLEAN NOT NULL DEFAULT false,

    -- ~~~ Category assignment ~~~
    category_id            INTEGER REFERENCES categories(category_id),
    category_source        VARCHAR
        CHECK (category_source IN ('rule','llm','manual')),
    -- Which rule fired, keyed into config/rules/categories.yaml, so a wrong
    -- category resolves to the line that caused it rather than to a guess.
    category_rule_key      VARCHAR,
    category_assigned_at   TIMESTAMPTZ,

    -- The most recent proposal that LOST to the assignment above. One slot,
    -- overwritten each time: enough to see that a pass wanted something else,
    -- without becoming a suggestion queue.
    category_declined_id     INTEGER REFERENCES categories(category_id),
    category_declined_source VARCHAR
        CHECK (category_declined_source IN ('rule','llm','manual')),
    category_declined_at     TIMESTAMPTZ,

    -- ~~~ Merchant assignment ~~~ same authority rule, same enforcement.
    merchant_id            INTEGER REFERENCES merchants(merchant_id),
    merchant_source        VARCHAR
        CHECK (merchant_source IN ('rule','llm','manual')),
    merchant_rule_key      VARCHAR,
    merchant_assigned_at   TIMESTAMPTZ,

    -- An assignment with no recorded source is the thing this whole block
    -- exists to prevent, so the two columns stand or fall together.
    CHECK ((category_id IS NULL) = (category_source IS NULL)),
    CHECK ((merchant_id IS NULL) = (merchant_source IS NULL)),

    -- Provenance. first_import_id is the run that introduced this row;
    -- last_import_id is the most recent run that saw it again. With
    -- source_line_no, a row resolves to a file digest and a line in it.
    first_import_id  INTEGER NOT NULL REFERENCES import_log(import_id),
    last_import_id   INTEGER NOT NULL REFERENCES import_log(import_id),
    source_line_no   INTEGER,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The dedupe guarantee. Scoped per account: two accounts can legitimately
    -- hold the same transaction content, and for a transfer they usually do.
    UNIQUE (account_id, dedupe_key)
);

-- ---------------------------------------------------------------------------
-- transfers: matched pairs of transactions that move value between the
-- captain's own accounts. Populated by a later change; declared now so the
-- schema leaves room for it, per DESIGN.md.
--
-- A transfer is not spending. Counting one as spending double-counts a card
-- payment against the purchases it settles.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transfers (
    transfer_id       INTEGER PRIMARY KEY DEFAULT nextval('seq_transfer_id'),
    from_transaction_id INTEGER NOT NULL UNIQUE REFERENCES transactions(transaction_id),
    to_transaction_id   INTEGER NOT NULL UNIQUE REFERENCES transactions(transaction_id),
    amount            DECIMAL(18,2) NOT NULL,  -- always positive: the magnitude moved
    posted_date       DATE NOT NULL,
    -- Days between the two legs. Card payments routinely settle a day or two
    -- apart, so the matcher tolerates a window rather than demanding equality.
    day_gap           INTEGER NOT NULL DEFAULT 0,
    match_method      VARCHAR NOT NULL DEFAULT 'amount_date'
        CHECK (match_method IN ('amount_date','rule','manual')),
    confidence        DOUBLE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- balances: point-in-time balance observations, one row per account per
-- as-of instant per source kind.
--
-- as_of is INCLUSIVE: the balance reflects every transaction posted on or
-- before that instant. Everything that writes this table obeys that, which is
-- why an opening balance derived from an OFX window is recorded at the day
-- BEFORE the window's DTSTART rather than at DTSTART itself. Getting this
-- wrong is a silent one-day error in every reconciliation.
--
-- Where the authoritative figure comes from, now that NFCU has dropped OFX:
-- the captain reads it off his monthly statement or the online balance on
-- download day and enters it by hand. That is source_kind 'stated'. The
-- OFX-parity check was a one-time proof that the adapter was right, not the
-- ongoing mechanism.
--
-- NOT YET BUILT: the CLI entry point that records a stated balance, and the
-- monthly check that compares it against derived opening + posted net. These
-- columns are the schema half, landed here while schema.sql is still
-- unmerged so that adding them later is not the project's first migration.
--
-- A stated balance is a real dollar figure, so it must NOT be moved into
-- config/, which is committed. It satisfies DESIGN.md's "human decisions must
-- not live only in the database" rule the other way the rule allows: it is
-- re-enterable, from a statement the captain holds, in one command.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS balances (
    balance_id   INTEGER PRIMARY KEY DEFAULT nextval('seq_balance_id'),
    account_id   INTEGER NOT NULL REFERENCES accounts(account_id),
    as_of        TIMESTAMPTZ NOT NULL,
    -- 'ledger'    posted balance. This is the one to reconcile against.
    -- 'available' nets pending holds the ledger has not yet posted, so it
    --             will NOT agree with a sum of posted transactions.
    -- 'derived'   computed by purser, recorded for audit, never authoritative.
    balance_type VARCHAR NOT NULL
        CHECK (balance_type IN ('ledger','available','derived')),
    -- 'file'    read out of an institution's own export.
    -- 'stated'  typed in by the captain from a statement or the online
    --           balance. Independent of anything purser computed, which is
    --           exactly what makes the monthly check able to fail.
    source_kind  VARCHAR NOT NULL DEFAULT 'file'
        CHECK (source_kind IN ('file','stated')),
    amount       DECIMAL(18,2) NOT NULL,
    currency     VARCHAR NOT NULL DEFAULT 'USD',
    source_file  VARCHAR,
    -- Where the figure came from, in the captain's words: "August statement
    -- p1", "online balance at download". Required for a stated balance.
    note         VARCHAR,
    import_id    INTEGER REFERENCES import_log(import_id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, as_of, balance_type, source_kind)
);

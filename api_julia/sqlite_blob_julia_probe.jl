using JSON3
using SQLite
using DBInterface
using Random
using Statistics
using Printf

"""
Small proof-of-concept runner:
1) SQLite returns postings/docpost blobs
2) Julia decodes + runs CNF OR/NEAR logic
3) Optionally fetches fragments back from SQLite

Usage:
  POSTINGS_CONFIG=/path/config.local.json julia api_julia/sqlite_blob_julia_probe.jl payload.json

Payload example:
{
  "termGroups": [["hamar", "lillehammer"], ["gjovik"]],
  "window": 15,
  "before": 15,
  "after": 15,
  "perBook": 2,
  "docSamples": 10,
  "totalLimit": 100,
  "maxVariants": 20,
  "mode": "fragments",   // count | hits | fragments
  "repeats": 3
}
"""

struct AppConfig
    postings_dbs::Vector{String}
    words_db::Union{Nothing, String}
end

function load_config()::AppConfig
    cfg_path = get(ENV, "POSTINGS_CONFIG", "")
    isempty(cfg_path) && error("POSTINGS_CONFIG is not set")
    cfg = JSON3.read(read(cfg_path, String))
    postings = String.(get(cfg, "postings_dbs", String[]))
    isempty(postings) && error("postings_dbs missing in config")
    words_db = String(get(cfg, "words_db", ""))
    return AppConfig(postings, isempty(words_db) ? nothing : words_db)
end

words_path(cfg::AppConfig, shard_path::String) = cfg.words_db === nothing ? shard_path : cfg.words_db

const LIBROARING = "libroaring"

function nonempty_json_array(x)::Bool
    if x === nothing
        return false
    end
    try
        return !isempty(x)
    catch
        return false
    end
end

function decode_postings_varint(blob)::Vector{Int}
    blob === nothing && return Int[]
    bytes = Vector{UInt8}(blob)
    out = Int[]
    prev = 0
    cur = 0
    shift = 0
    for b in bytes
        x = Int(b)
        cur |= (x & 0x7f) << shift
        if (x & 0x80) == 0
            prev += cur
            push!(out, prev)
            cur = 0
            shift = 0
        else
            shift += 7
        end
    end
    return out
end

function decode_postings_roaring(blob)::Vector{Int}
    blob === nothing && return Int[]
    bytes = Vector{UInt8}(blob)
    isempty(bytes) && return Int[]
    bm = ccall(
        (:roaring_bitmap_portable_deserialize_safe, LIBROARING),
        Ptr{Cvoid},
        (Ptr{UInt8}, Csize_t),
        pointer(bytes),
        Csize_t(length(bytes)),
    )
    bm == C_NULL && return Int[]
    try
        card = ccall((:roaring_bitmap_get_cardinality, LIBROARING), UInt64, (Ptr{Cvoid},), bm)
        card == 0 && return Int[]
        arr = Vector{UInt32}(undef, Int(card))
        ccall(
            (:roaring_bitmap_to_uint32_array, LIBROARING),
            Cvoid,
            (Ptr{Cvoid}, Ptr{UInt32}),
            bm,
            pointer(arr),
        )
        return Int.(arr)
    finally
        ccall((:roaring_bitmap_free, LIBROARING), Cvoid, (Ptr{Cvoid},), bm)
    end
end

function decode_postings(blob, codec::Symbol)::Vector{Int}
    if codec == :roaring
        return decode_postings_roaring(blob)
    end
    return decode_postings_varint(blob)
end

function detect_postings_codec(db)::Symbol
    try
        q = DBInterface.execute(db, "SELECT value FROM meta WHERE key = 'postings_codec' LIMIT 1")
        it = iterate(q)
        if it !== nothing
            v = String(it[1][1])
            if lowercase(strip(v)) == "roaring_v1"
                return :roaring
            end
        end
    catch
    end
    return :varint
end

function merge_sorted_unique(a::Vector{Int}, b::Vector{Int})::Vector{Int}
    out = Int[]
    i = 1
    j = 1
    last = typemin(Int)
    while i <= length(a) || j <= length(b)
        v =
            if j > length(b) || (i <= length(a) && a[i] <= b[j])
                x = a[i]
                i += 1
                x
            else
                x = b[j]
                j += 1
                x
            end
        if v != last
            push!(out, v)
            last = v
        end
    end
    return out
end

function near_anchor(anchor::Vector{Int}, other::Vector{Int}, off_min::Int, off_max::Int)::Vector{Int}
    isempty(anchor) && return Int[]
    isempty(other) && return Int[]
    out = Int[]
    j = 1
    for a in anchor
        lo = a + off_min
        hi = a + off_max
        while j <= length(other) && other[j] < lo
            j += 1
        end
        if j <= length(other) && other[j] <= hi
            push!(out, a)
        end
    end
    return out
end

function cf_ids_for_term(words_db, term::String, max_variants::Int)::Vector{Int}
    t = lowercase(strip(term))
    isempty(t) && return Int[]
    if endswith(t, "*")
        prefix = t[1:end-1]
        isempty(prefix) && return Int[]
        sql = """
        SELECT cf_id
        FROM words
        WHERE word >= ? AND word < ?
        GROUP BY cf_id
        ORDER BY total_tf DESC
        LIMIT ?
        """
        rows = DBInterface.execute(words_db, sql, (prefix, string(prefix, '\uffff'), max_variants))
        return [Int(r[1]) for r in rows]
    end
    q = DBInterface.execute(
        words_db, "SELECT cf_id FROM words WHERE word = ? ORDER BY raw_id LIMIT 1", (t,)
    )
    it = iterate(q)
    row = it === nothing ? nothing : it[1]
    return row === nothing ? Int[] : [Int(row[1])]
end

function group_doc_ids(words_db, cf_ids::Vector{Int}, codec::Symbol)::Union{Nothing, Set{Int}}
    # Returns:
    # - Set{Int}: if docpost is available
    # - nothing:  if docpost prefilter cannot be used for this group
    isempty(cf_ids) && return Set{Int}()
    ph = join(fill("?", length(cf_ids)), ",")
    sql = "SELECT docpost, docpost_is_complement FROM words WHERE cf_id IN ($ph)"
    out = Set{Int}()
    had_blob = false
    try
        rows = DBInterface.execute(words_db, sql, Tuple(cf_ids))
        for r in rows
            blob = r[1]
            is_comp = Int(r[2])
            if blob === nothing || is_comp == 1
                continue
            end
            had_blob = true
            for id in decode_postings(blob, codec)
                push!(out, id)
            end
        end
    catch
        return nothing
    end
    return had_blob ? out : nothing
end

function fetch_group_positions(
    shard_db,
    cf_ids::Vector{Int},
    candidate_books::Union{Nothing, Vector{Int}},
    codec::Symbol,
)::Dict{Int, Vector{Int}}
    out = Dict{Int, Vector{Int}}()
    isempty(cf_ids) && return out
    ph_cf = join(fill("?", length(cf_ids)), ",")
    params = Any[cf_ids...]
    if candidate_books === nothing
        sql = "SELECT book_id, post FROM unigrams WHERE cf_id IN ($ph_cf)"
    else
        isempty(candidate_books) && return out
        ph_books = join(fill("?", length(candidate_books)), ",")
        sql = "SELECT book_id, post FROM unigrams WHERE cf_id IN ($ph_cf) AND book_id IN ($ph_books)"
        append!(params, candidate_books)
    end
    rows = DBInterface.execute(shard_db, sql, Tuple(params))
    for r in rows
        book_id = Int(r[1])
        pos = decode_postings(r[2], codec)
        isempty(pos) && continue
        if haskey(out, book_id)
            out[book_id] = merge_sorted_unique(out[book_id], pos)
        else
            out[book_id] = pos
        end
    end
    return out
end

function build_groups(payload)::Vector{Vector{String}}
    if haskey(payload, :termGroups) && nonempty_json_array(payload.termGroups)
        return [[String(t) for t in g] for g in payload.termGroups]
    end
    terms = get(payload, :terms, nothing)
    if !nonempty_json_array(terms)
        return Vector{Vector{String}}()
    end
    return [[String(t)] for t in terms]
end

function common_books(group_maps::Vector{Dict{Int, Vector{Int}}})::Vector{Int}
    isempty(group_maps) && return Int[]
    ids = Set(keys(group_maps[1]))
    for gm in group_maps[2:end]
        ids = intersect(ids, Set(keys(gm)))
        isempty(ids) && return Int[]
    end
    out = collect(ids)
    sort!(out)
    return out
end

function fetch_fragment(shard_db, words_db, book_id::Int, center::Int, before::Int, after::Int)::String
    lo = max(center - before, 0)
    hi = center + after
    rows = DBInterface.execute(
        shard_db,
        """
        SELECT seq, raw_id
        FROM tokens
        WHERE book_id = ? AND seq BETWEEN ? AND ?
        ORDER BY seq
        """,
        (book_id, lo, hi),
    )
    seqs = Int[]
    raw_ids = Int[]
    for r in rows
        push!(seqs, Int(r[1]))
        push!(raw_ids, Int(r[2]))
    end
    isempty(raw_ids) && return ""
    ph = join(fill("?", length(raw_ids)), ",")
    raw_map = Dict{Int, String}()
    for r in DBInterface.execute(words_db, "SELECT raw_id, word FROM words WHERE raw_id IN ($ph)", Tuple(raw_ids))
        raw_map[Int(r[1])] = String(r[2])
    end
    tokens = String[]
    for (seq, rid) in zip(seqs, raw_ids)
        w = get(raw_map, rid, "?")
        push!(tokens, seq == center ? "[$w]" : w)
    end
    return join(tokens, " ")
end

function process_shard(
    shard_path::String,
    cfg::AppConfig,
    groups::Vector{Vector{String}},
    max_variants::Int,
    doc_samples::Int,
    per_book::Int,
    total_limit::Int,
    off_min::Int,
    off_max::Int,
    mode::String,
    before::Int,
    after::Int,
    rng::AbstractRNG,
    base_filter_ids::Union{Nothing, Vector{Int}},
)
    phase_ms = Dict(
        "resolve_terms_ms" => 0.0,
        "docprefilter_ms" => 0.0,
        "fetch_positions_ms" => 0.0,
        "near_core_ms" => 0.0,
        "fragments_ms" => 0.0,
    )
    total = 0
    docs = 0
    out_rows = Vector{Dict{String, Any}}()

    shard_db = SQLite.DB(shard_path)
    words_db = SQLite.DB(words_path(cfg, shard_path))
    shard_codec = detect_postings_codec(shard_db)
    words_codec = detect_postings_codec(words_db)

    tr = time_ns()
    group_cf_ids = Vector{Vector{Int}}()
    for g in groups
        ids = Int[]
        for term in g
            append!(ids, cf_ids_for_term(words_db, term, max_variants))
        end
        ids = unique(ids)
        if isempty(ids)
            group_cf_ids = Vector{Vector{Int}}()
            break
        end
        push!(group_cf_ids, ids)
    end
    phase_ms["resolve_terms_ms"] += (time_ns() - tr) / 1e6
    if isempty(group_cf_ids)
        SQLite.close(shard_db)
        SQLite.close(words_db)
        return Dict(
            "total" => total,
            "docs" => docs,
            "rows" => out_rows,
            "timings_ms" => phase_ms,
            "shard" => shard_path,
        )
    end

    td = time_ns()
    candidate::Union{Nothing, Vector{Int}} = nothing
    usable_prefilter = true
    doc_sets = Vector{Set{Int}}()
    for ids in group_cf_ids
        s = group_doc_ids(words_db, ids, words_codec)
        if s === nothing
            usable_prefilter = false
            break
        end
        push!(doc_sets, s)
    end
    if usable_prefilter && !isempty(doc_sets)
        ids = reduce(intersect, doc_sets)
        candidate = collect(ids)
        sort!(candidate)
    end
    if base_filter_ids !== nothing
        filter_set = Set(base_filter_ids)
        if candidate === nothing
            candidate = [bid for bid in base_filter_ids if bid in filter_set]
        else
            candidate = [bid for bid in candidate if bid in filter_set]
        end
        sort!(candidate)
    end
    if candidate !== nothing && doc_samples > 0 && length(candidate) > doc_samples
        candidate = rand(rng, candidate, doc_samples)
    end
    phase_ms["docprefilter_ms"] += (time_ns() - td) / 1e6

    tf = time_ns()
    group_maps = [fetch_group_positions(shard_db, ids, candidate, shard_codec) for ids in group_cf_ids]
    books = common_books(group_maps)
    phase_ms["fetch_positions_ms"] += (time_ns() - tf) / 1e6

    tn = time_ns()
    hit_rows = Vector{Tuple{Int, Int}}() # book_id, seq
    for book_id in books
        anchor = group_maps[1][book_id]
        for i in 2:length(group_maps)
            anchor = near_anchor(anchor, group_maps[i][book_id], off_min, off_max)
            isempty(anchor) && break
        end
        c = length(anchor)
        c == 0 && continue
        total += c
        docs += 1
        if mode != "count"
            if c > per_book
                idx = randperm(rng, c)[1:per_book]
                for k in idx
                    push!(hit_rows, (book_id, anchor[k]))
                end
            else
                for seq in anchor
                    push!(hit_rows, (book_id, seq))
                end
            end
            if total_limit > 0 && length(out_rows) + length(hit_rows) >= total_limit
                break
            end
        end
    end
    phase_ms["near_core_ms"] += (time_ns() - tn) / 1e6

    if mode == "hits"
        for (book_id, seq) in hit_rows
            push!(out_rows, Dict("bookId" => book_id, "seq" => seq, "shard" => shard_path))
            if total_limit > 0 && length(out_rows) >= total_limit
                break
            end
        end
    elseif mode == "fragments"
        tfra = time_ns()
        for (book_id, seq) in hit_rows
            frag = fetch_fragment(shard_db, words_db, book_id, seq, before, after)
            push!(out_rows, Dict("bookId" => book_id, "seq" => seq, "frag" => frag, "shard" => shard_path))
            if total_limit > 0 && length(out_rows) >= total_limit
                break
            end
        end
        phase_ms["fragments_ms"] += (time_ns() - tfra) / 1e6
    end

    SQLite.close(shard_db)
    SQLite.close(words_db)
    return Dict(
        "total" => total,
        "docs" => docs,
        "rows" => out_rows,
        "timings_ms" => phase_ms,
        "shard" => shard_path,
    )
end

function run_once(payload, cfg::AppConfig, rng::AbstractRNG)
    groups = build_groups(payload)
    length(groups) < 2 && error("Need at least two groups/terms")

    window = Int(get(payload, :window, 5))
    symmetric = Bool(get(payload, :symmetric, true))
    off_min = symmetric ? -window : 1
    off_max = window

    before = Int(get(payload, :before, 10))
    after = Int(get(payload, :after, 10))
    per_book = Int(get(payload, :perBook, 2))
    doc_samples = Int(get(payload, :docSamples, 0))
    total_limit = Int(get(payload, :totalLimit, 100))
    max_variants = Int(get(payload, :maxVariants, 10))
    mode = String(get(payload, :mode, "hits")) # count | hits | fragments
    parallel_shards = Bool(get(payload, :parallelShards, false))
    use_filter = Bool(get(payload, :useFilter, false))
    filter_ids_payload = get(payload, :filterIds, Int[])
    base_filter_ids::Union{Nothing, Vector{Int}} = nothing
    if use_filter
        base_filter_ids = [Int(x) for x in filter_ids_payload]
    end

    t0 = time_ns()
    total = 0
    docs = 0
    out_rows = Vector{Dict{String, Any}}()

    phase_ms = Dict(
        "resolve_terms_ms" => 0.0,
        "docprefilter_ms" => 0.0,
        "fetch_positions_ms" => 0.0,
        "near_core_ms" => 0.0,
        "fragments_ms" => 0.0,
    )

    shard_results = Vector{Dict{String, Any}}()
    if parallel_shards
        # One task per shard (single process, multithreaded tasks).
        # Keep DB handles task-local; never share SQLite.DB across tasks.
        base_seed = rand(rng, UInt)
        tasks = map(enumerate(cfg.postings_dbs)) do (i, shard_path)
            Threads.@spawn begin
                shard_rng = MersenneTwister(hash((base_seed, i, shard_path)))
                process_shard(
                    shard_path,
                    cfg,
                    groups,
                    max_variants,
                    doc_samples,
                    per_book,
                    total_limit,
                    off_min,
                    off_max,
                    mode,
                    before,
                    after,
                    shard_rng,
                base_filter_ids,
                )
            end
        end
        for t in tasks
            push!(shard_results, fetch(t))
        end
    else
        for shard_path in cfg.postings_dbs
            push!(
                shard_results,
                process_shard(
                    shard_path,
                    cfg,
                    groups,
                    max_variants,
                    doc_samples,
                    per_book,
                    total_limit,
                    off_min,
                    off_max,
                    mode,
                    before,
                    after,
                    rng,
                    base_filter_ids,
                ),
            )
        end
    end

    for s in shard_results
        total += Int(s["total"])
        docs += Int(s["docs"])
        timings = s["timings_ms"]
        phase_ms["resolve_terms_ms"] += Float64(timings["resolve_terms_ms"])
        phase_ms["docprefilter_ms"] += Float64(timings["docprefilter_ms"])
        phase_ms["fetch_positions_ms"] += Float64(timings["fetch_positions_ms"])
        phase_ms["near_core_ms"] += Float64(timings["near_core_ms"])
        phase_ms["fragments_ms"] += Float64(timings["fragments_ms"])
        if mode != "count"
            append!(out_rows, s["rows"])
            if total_limit > 0 && length(out_rows) > total_limit
                resize!(out_rows, total_limit)
            end
        end
        if total_limit > 0 && length(out_rows) >= total_limit
            break
        end
    end

    total_ms = (time_ns() - t0) / 1e6
    return Dict(
        "total" => total,
        "docs" => docs,
        "rows" => out_rows,
        "timings_ms" => merge(
            phase_ms,
            Dict(
                "total_ms" => total_ms,
                "parallel_shards" => parallel_shards,
                "threads_nthreads" => Threads.nthreads(),
            ),
        ),
    )
end

function main()
    if length(ARGS) < 1
        error("Usage: julia api_julia/sqlite_blob_julia_probe.jl payload.json")
    end
    payload = JSON3.read(read(ARGS[1], String))
    cfg = load_config()
    repeats = Int(get(payload, :repeats, 1))
    rng = MersenneTwister(42)

    runs = Vector{Dict{String, Any}}()
    for _ in 1:repeats
        push!(runs, run_once(payload, cfg, rng))
    end

    total_ms = [Float64(r["timings_ms"]["total_ms"]) for r in runs]
    out = Dict(
        "repeats" => repeats,
        "avg_total_ms" => mean(total_ms),
        "median_total_ms" => median(total_ms),
        "last_run" => runs[end],
    )
    println(JSON3.write(out))
end

if abspath(PROGRAM_FILE) == @__FILE__
    main()
end

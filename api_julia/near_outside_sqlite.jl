using JSON3
using SQLite
using DBInterface
using Random

function load_config()
    cfg_path = get(ENV, "POSTINGS_CONFIG", "")
    isempty(cfg_path) && error("POSTINGS_CONFIG is not set.")
    data = JSON3.read(read(cfg_path, String))
    postings = String.(get(data, "postings_dbs", String[]))
    isempty(postings) && error("postings_dbs is required in config.")
    return postings
end

function decode_postings(blob)::Vector{Int}
    blob === nothing && return Int[]
    bytes = Vector{UInt8}(blob)
    out = Int[]
    prev = 0
    x = 0
    shift = 0
    for b in bytes
        bi = Int(b)
        x |= (bi & 0x7f) << shift
        if (bi & 0x80) == 0
            prev += x
            push!(out, prev)
            x = 0
            shift = 0
        else
            shift += 7
        end
    end
    return out
end

function merge_sorted(a::Vector{Int}, b::Vector{Int})::Vector{Int}
    i = 1
    j = 1
    out = Int[]
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

function cf_ids_for_term(db, term::String, max_variants::Int)::Vector{Int}
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
        rows = DBInterface.execute(db, sql, (prefix, string(prefix, '\uffff'), max_variants))
        return [Int(r[1]) for r in rows]
    end
    q = DBInterface.execute(db, "SELECT cf_id FROM words WHERE word = ? ORDER BY raw_id LIMIT 1", (t,))
    st = iterate(q)
    row = st === nothing ? nothing : st[1]
    return row === nothing ? Int[] : [Int(row[1])]
end

function build_groups(payload)::Vector{Vector{String}}
    if haskey(payload, :termGroups)
        return [[String(x) for x in g] for g in payload.termGroups]
    end
    terms = get(payload, :terms, String[])
    return [[String(t)] for t in terms]
end

function fetch_group_positions(db, cf_ids::Vector{Int}, candidate_books::Union{Nothing, Vector{Int}}=nothing)::Dict{Int, Vector{Int}}
    out = Dict{Int, Vector{Int}}()
    isempty(cf_ids) && return out
    placeholders_cf = join(fill("?", length(cf_ids)), ",")
    params = Any[cf_ids...]
    if candidate_books !== nothing
        isempty(candidate_books) && return out
        placeholders_books = join(fill("?", length(candidate_books)), ",")
        sql = "SELECT book_id, post FROM unigrams WHERE cf_id IN ($placeholders_cf) AND book_id IN ($placeholders_books)"
        append!(params, candidate_books)
    else
        sql = "SELECT book_id, post FROM unigrams WHERE cf_id IN ($placeholders_cf)"
    end
    rows = DBInterface.execute(db, sql, Tuple(params))
    for r in rows
        book_id = Int(r[1])
        positions = decode_postings(r[2])
        if isempty(positions)
            continue
        end
        if haskey(out, book_id)
            out[book_id] = merge_sorted(out[book_id], positions)
        else
            out[book_id] = positions
        end
    end
    return out
end

function common_book_ids(group_maps::Vector{Dict{Int, Vector{Int}}})::Vector{Int}
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

function group_doc_ids(db, cf_ids::Vector{Int})::Set{Int}
    out = Set{Int}()
    isempty(cf_ids) && return out
    placeholders = join(fill("?", length(cf_ids)), ",")
    sql = "SELECT docpost, docpost_is_complement FROM words WHERE cf_id IN ($placeholders)"
    rows = DBInterface.execute(db, sql, Tuple(cf_ids))
    for r in rows
        blob = r[1]
        is_comp = Int(r[2])
        if blob === nothing || is_comp == 1
            continue
        end
        ids = decode_postings(blob)
        for id in ids
            push!(out, id)
        end
    end
    return out
end

function run_payload(payload)
    groups = build_groups(payload)
    length(groups) < 2 && error("Need at least two groups/terms.")
    window = Int(get(payload, :window, 5))
    symmetric = Bool(get(payload, :symmetric, true))
    max_variants = Int(get(payload, :maxVariants, 10))
    doc_samples = Int(get(payload, :docSamples, 0))
    per_book = Int(get(payload, :perBook, 2))
    total_limit = Int(get(payload, :totalLimit, 200))
    mode = String(get(payload, :mode, "list"))  # list | count
    off_min = symmetric ? -window : 1
    off_max = window

    totals = 0
    docs = 0
    hits = Vector{Dict{String, Int}}()

    for db_path in load_config()
        db = SQLite.DB(db_path)
        group_cf_ids = [unique(reduce(vcat, [cf_ids_for_term(db, t, max_variants) for t in g]; init=Int[])) for g in groups]
        if any(isempty, group_cf_ids)
            SQLite.close(db)
            continue
        end
        doc_sets = [group_doc_ids(db, ids) for ids in group_cf_ids]
        candidate = collect(reduce(intersect, doc_sets))
        sort!(candidate)
        group_maps = [fetch_group_positions(db, ids, candidate) for ids in group_cf_ids]
        books = common_book_ids(group_maps)
        if doc_samples > 0 && length(books) > doc_samples
            books = Random.shuffle(books)[1:doc_samples]
        end
        for book_id in books
            anchor = group_maps[1][book_id]
            for i in 2:length(group_maps)
                anchor = near_anchor(anchor, group_maps[i][book_id], off_min, off_max)
                isempty(anchor) && break
            end
            c = length(anchor)
            if c <= 0
                continue
            end
            totals += c
            docs += 1
            if mode == "list"
                for seq in anchor[1:min(per_book, c)]
                    push!(hits, Dict("bookId" => book_id, "seq" => seq))
                    if total_limit > 0 && length(hits) >= total_limit
                        break
                    end
                end
            end
            if total_limit > 0 && length(hits) >= total_limit
                break
            end
        end
        SQLite.close(db)
        if total_limit > 0 && length(hits) >= total_limit
            break
        end
    end

    if mode == "count"
        println(JSON3.write(Dict("total" => totals, "docs" => docs)))
    else
        println(JSON3.write(Dict("total" => totals, "docs" => docs, "hits" => hits)))
    end
end

function main()
    if length(ARGS) < 1
        error("Usage: julia api_julia/near_outside_sqlite.jl /path/payload.json")
    end
    payload = JSON3.read(read(ARGS[1], String))
    run_payload(payload)
end

main()

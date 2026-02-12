using HTTP
using JSON3
using SQLite
using DBInterface
using Random


struct AppConfig
    postings_dbs::Vector{String}
    words_db::Union{Nothing, String}
    ext_path::String
    default_schema::String
end


function load_config()::AppConfig
    cfg_path = get(ENV, "POSTINGS_CONFIG", "")
    isempty(cfg_path) && error("POSTINGS_CONFIG is not set.")
    data = JSON3.read(read(cfg_path, String))
    postings = get(data, "postings_dbs", [])
    isempty(postings) && error("postings_dbs is required in config.")
    words_db = get(data, "words_db", "")
    ext_path = get(data, "ext_path", "")
    default_schema = get(data, "default_schema", "unigrams")
    for path in postings
        isfile(String(path)) || error("postings_db not found: $(path)")
    end
    if !isempty(words_db)
        isfile(String(words_db)) || error("words_db not found: $(words_db)")
    end
    return AppConfig(
        String.(postings),
        isempty(words_db) ? nothing : String(words_db),
        String(ext_path),
        String(default_schema),
    )
end


const CONFIG = load_config()


function connect_postings(db_path::String, ext_path::String)
    db = SQLite.DB("file:$(db_path)?mode=ro"; uri=true)
    if !isempty(ext_path)
        DBInterface.execute(db, "SELECT load_extension(?, ?)", (ext_path, "sqlite3_postings_init"))
    end
    return db
end


function connect_words(db_path::String)
    return SQLite.DB("file:$(db_path)?mode=ro"; uri=true)
end


function shard_words_path(postings_path::String)
    return CONFIG.words_db === nothing ? postings_path : CONFIG.words_db
end


function ensure_urn_filter(db, urns::Vector{Int})
    DBInterface.execute(db, "DROP TABLE IF EXISTS urn_filter;")
    DBInterface.execute(db, "CREATE TEMP TABLE urn_filter (urn INTEGER PRIMARY KEY) WITHOUT ROWID;")
    stmt = DBInterface.prepare(db, "INSERT INTO urn_filter(urn) VALUES (?)")
    for u in urns
        DBInterface.execute(stmt, (u,))
    end
end


function get_cf_id(db, word::String)
    w = lowercase(word)
    row = first(DBInterface.execute(db, "SELECT cf_id FROM words WHERE word = ? ORDER BY raw_id LIMIT 1", (w,)), nothing)
    if row !== nothing
        return row[1]
    end
    row = first(DBInterface.execute(db, "SELECT cf_id FROM words WHERE word = ? ORDER BY raw_id LIMIT 1", (word,)), nothing)
    return row === nothing ? nothing : row[1]
end


function raw_words(db, raw_ids::Vector{Int})
    isempty(raw_ids) && return Dict{Int, String}()
    placeholders = join(fill("?", length(raw_ids)), ",")
    sql = "SELECT raw_id, word FROM words WHERE raw_id IN ($(placeholders))"
    rows = DBInterface.execute(db, sql, Tuple(raw_ids))
    out = Dict{Int, String}()
    for row in rows
        out[row[1]] = row[2]
    end
    return out
end


function fetch_window(db, words_db, book_id::Int, center::Int, before::Int, after::Int)
    start = max(center - before, 0)
    finish = center + after
    rows = DBInterface.execute(
        db,
        """
        SELECT seq, raw_id
        FROM tokens
        WHERE book_id = ? AND seq BETWEEN ? AND ?
        ORDER BY seq
        """,
        (book_id, start, finish),
    )
    raw_ids = Int[]
    seqs = Int[]
    for row in rows
        push!(seqs, row[1])
        push!(raw_ids, row[2])
    end
    raw_map = raw_words(words_db, raw_ids)
    tokens = String[]
    for (seq, raw_id) in zip(seqs, raw_ids)
        w = get(raw_map, raw_id, "?")
        if seq == center
            push!(tokens, "[$w]")
        else
            push!(tokens, w)
        end
    end
    return join(tokens, " ")
end


function sample_concordance_single(db, words_db, cf_id::Int, per_book::Int, before::Int, after::Int, use_filter::Bool)
    sql = use_filter ?
        """
        SELECT u.book_id, u.tf, u.post
        FROM unigrams u
        JOIN urn_filter f ON f.urn = u.book_id
        WHERE u.cf_id = ?
        """ :
        "SELECT book_id, tf, post FROM unigrams WHERE cf_id = ?"
    out = Vector{Tuple{Int, Int, String}}()
    for row in DBInterface.execute(db, sql, (cf_id,))
        book_id = row[1]
        tf = row[2]
        post = row[3]
        tf <= 0 && continue
        samples = min(per_book, tf)
        for _ in 1:samples
            idx = rand(0:tf-1)
            sample_row = first(DBInterface.execute(db, "SELECT post_sample(?, ?)", (post, idx)), nothing)
            sample_row === nothing && continue
            pos = Int(sample_row[1])
            frag = fetch_window(db, words_db, book_id, pos, before, after)
            push!(out, (book_id, pos, frag))
        end
    end
    return out
end


function sample_concordance_near(
    db,
    words_db,
    cf_a::Int,
    cf_b::Int,
    per_book::Int,
    before::Int,
    after::Int,
    use_filter::Bool,
    ngrams_table::String,
    off_min::Int,
    off_max::Int,
    exclude_self::Bool,
)
    sql = use_filter ?
        """
        SELECT a.book_id, a.post, b.post
        FROM $(ngrams_table) a
        JOIN $(ngrams_table) b ON a.book_id = b.book_id
        JOIN urn_filter f ON f.urn = a.book_id
        WHERE a.cf_id = ? AND b.cf_id = ?
        """ :
        """
        SELECT a.book_id, a.post, b.post
        FROM $(ngrams_table) a
        JOIN $(ngrams_table) b ON a.book_id = b.book_id
        WHERE a.cf_id = ? AND b.cf_id = ?
        """
    out = Vector{Tuple{Int, Int, String}}()
    for row in DBInterface.execute(db, sql, (cf_a, cf_b))
        book_id = row[1]
        post_a = row[2]
        post_b = row[3]
        if exclude_self && cf_a == cf_b && off_min == 0 && off_max == 0
            pos_row = first(DBInterface.execute(db, "SELECT post_near_positions(?, ?, ?, ?)", (post_a, post_b, 1, 1)), nothing)
            pos_row === nothing && continue
            positions = JSON3.read(String(pos_row[1]))
        else
            pos_row = first(DBInterface.execute(db, "SELECT post_near_positions(?, ?, ?, ?)", (post_a, post_b, off_min, off_max)), nothing)
            pos_row === nothing && continue
            positions = isempty(pos_row[1]) ? Int[] : JSON3.read(String(pos_row[1]))
        end
        isempty(positions) && continue
        samples = min(per_book, length(positions))
        for pos in Random.sample(positions, samples)
            frag = fetch_window(db, words_db, book_id, Int(pos), before, after)
            push!(out, (book_id, Int(pos), frag))
        end
    end
    return out
end


function near_frequency(
    db,
    cf_a::Int,
    cf_b::Int,
    window::Int,
    use_filter::Bool,
    ngrams_table::String,
    symmetric::Bool,
    exclude_self::Bool,
)
    sql = use_filter ?
        """
        SELECT a.book_id, a.post, b.post
        FROM $(ngrams_table) a
        JOIN $(ngrams_table) b ON a.book_id = b.book_id
        JOIN urn_filter f ON f.urn = a.book_id
        WHERE a.cf_id = ? AND b.cf_id = ?
        """ :
        """
        SELECT a.book_id, a.post, b.post
        FROM $(ngrams_table) a
        JOIN $(ngrams_table) b ON a.book_id = b.book_id
        WHERE a.cf_id = ? AND b.cf_id = ?
        """
    total = 0
    docs = 0
    for row in DBInterface.execute(db, sql, (cf_a, cf_b))
        post_a = row[2]
        post_b = row[3]
        cnt = 0
        if cf_a == cf_b
            if exclude_self
                cnt = first(DBInterface.execute(db, "SELECT post_near_count(?, ?, 1, ?)", (post_a, post_b, window)))[1]
            elseif symmetric
                cnt = first(DBInterface.execute(db, "SELECT post_intersect_offset_sym(?, ?, ?, ?)", (post_a, post_b, -window, window)))[1]
            else
                cnt = first(DBInterface.execute(db, "SELECT post_near_count(?, ?, 1, ?)", (post_a, post_b, window)))[1]
            end
        else
            if symmetric
                cnt_ab = first(DBInterface.execute(db, "SELECT post_near_count(?, ?, 1, ?)", (post_a, post_b, window)))[1]
                cnt_ba = first(DBInterface.execute(db, "SELECT post_near_count(?, ?, 1, ?)", (post_b, post_a, window)))[1]
                cnt = cnt_ab + cnt_ba
            else
                cnt = first(DBInterface.execute(db, "SELECT post_near_count(?, ?, 1, ?)", (post_a, post_b, window)))[1]
            end
        end
        total += cnt
        if cnt > 0
            docs += 1
        end
    end
    return total, docs
end


function sample_collocations(
    db,
    words_db,
    cf_id::Int,
    per_book::Int,
    before::Int,
    after::Int,
    use_filter::Bool,
    ngrams_table::String,
)
    sql = use_filter ?
        """
        SELECT u.book_id, u.tf, u.post
        FROM $(ngrams_table) u
        JOIN urn_filter f ON f.urn = u.book_id
        WHERE u.cf_id = ?
        """ :
        "SELECT book_id, tf, post FROM $(ngrams_table) WHERE cf_id = ?"
    counts = Dict{String, Int}()
    for row in DBInterface.execute(db, sql, (cf_id,))
        book_id = row[1]
        tf = row[2]
        post = row[3]
        tf <= 0 && continue
        samples = min(per_book, tf)
        for _ in 1:samples
            idx = rand(0:tf-1)
            sample_row = first(DBInterface.execute(db, "SELECT post_sample(?, ?)", (post, idx)), nothing)
            sample_row === nothing && continue
            pos = Int(sample_row[1])
            rows = DBInterface.execute(
                db,
                """
                SELECT raw_id
                FROM tokens
                WHERE book_id = ? AND seq BETWEEN ? AND ?
                ORDER BY seq
                """,
                (book_id, max(pos - before, 0), pos + after),
            )
            raw_ids = Int[]
            for r in rows
                push!(raw_ids, r[1])
            end
            raw_map = raw_words(words_db, raw_ids)
            for raw_id in raw_ids
                w = lowercase(get(raw_map, raw_id, "?"))
                counts[w] = get(counts, w, 0) + 1
            end
        end
    end
    return counts
end


function json_response(obj, status::Int=200)
    return HTTP.Response(status, JSON3.write(obj), ["Content-Type" => "application/json"])
end


function parse_body(req::HTTP.Request)
    body = String(req.body)
    return isempty(body) ? Dict() : JSON3.read(body)
end


function handle_concordance(req::HTTP.Request)
    data = parse_body(req)
    wordA = get(data, "wordA", "")
    wordB = get(data, "wordB", "")
    before = Int(get(data, "before", 5))
    after = Int(get(data, "after", 5))
    perBook = Int(get(data, "perBook", 3))
    schema = String(get(data, "schema", CONFIG.default_schema))
    useFilter = Bool(get(data, "useFilter", false))
    filterIds = Vector{Int}(get(data, "filterIds", Int[]))
    symmetric = Bool(get(data, "symmetric", true))
    excludeSelf = Bool(get(data, "excludeSelf", false))

    rows = []
    word_a_found = false
    word_b_found = true
    for path in CONFIG.postings_dbs
        db = connect_postings(path, CONFIG.ext_path)
        wdb = connect_words(shard_words_path(path))
        if useFilter && !isempty(filterIds)
            ensure_urn_filter(db, filterIds)
        end
        cf_a = get_cf_id(wdb, wordA)
        if cf_a === nothing
            SQLite.close(db)
            SQLite.close(wdb)
            continue
        end
        word_a_found = true
        if !isempty(strip(String(wordB)))
            cf_b = get_cf_id(wdb, wordB)
            if cf_b === nothing
                word_b_found = false
                SQLite.close(db)
                SQLite.close(wdb)
                continue
            end
            if symmetric
                off_min, off_max = -before, after
            else
                off_min, off_max = 1, after
            end
            shard_rows = sample_concordance_near(
                db,
                wdb,
                cf_a,
                cf_b,
                perBook,
                before,
                after,
                useFilter && !isempty(filterIds),
                schema,
                off_min,
                off_max,
                excludeSelf,
            )
            for (b, p, f) in shard_rows
                push!(rows, Dict("bookId" => b, "pos" => p, "frag" => f))
            end
        else
            shard_rows = sample_concordance_single(
                db, wdb, cf_a, perBook, before, after, useFilter && !isempty(filterIds)
            )
            for (b, p, f) in shard_rows
                push!(rows, Dict("bookId" => b, "pos" => p, "frag" => f))
            end
        end
        SQLite.close(db)
        SQLite.close(wdb)
    end
    if !word_a_found
        return json_response(Dict("error" => "Word A not found"), 404)
    end
    if !isempty(strip(String(wordB))) && !word_b_found
        return json_response(Dict("error" => "Word B not found"), 404)
    end
    return json_response(Dict("rows" => rows))
end


function handle_near_frequency(req::HTTP.Request)
    data = parse_body(req)
    wordA = get(data, "wordA", "")
    wordB = get(data, "wordB", "")
    window = Int(get(data, "window", 5))
    schema = String(get(data, "schema", CONFIG.default_schema))
    useFilter = Bool(get(data, "useFilter", false))
    filterIds = Vector{Int}(get(data, "filterIds", Int[]))
    symmetric = Bool(get(data, "symmetric", true))
    excludeSelf = Bool(get(data, "excludeSelf", false))

    total = 0
    docs = 0
    found_any = false
    for path in CONFIG.postings_dbs
        db = connect_postings(path, CONFIG.ext_path)
        wdb = connect_words(shard_words_path(path))
        if useFilter && !isempty(filterIds)
            ensure_urn_filter(db, filterIds)
        end
        cf_a = get_cf_id(wdb, wordA)
        cf_b = get_cf_id(wdb, wordB)
        if cf_a === nothing || cf_b === nothing
            SQLite.close(db)
            SQLite.close(wdb)
            continue
        end
        found_any = true
        shard_total, shard_docs = near_frequency(
            db,
            cf_a,
            cf_b,
            window,
            useFilter && !isempty(filterIds),
            schema,
            symmetric,
            excludeSelf,
        )
        total += shard_total
        docs += shard_docs
        SQLite.close(db)
        SQLite.close(wdb)
    end
    if !found_any
        return json_response(Dict("error" => "Word not found"), 404)
    end
    return json_response(Dict("total" => total, "docs" => docs))
end


function handle_collocations(req::HTTP.Request)
    data = parse_body(req)
    word = get(data, "word", "")
    before = Int(get(data, "before", 5))
    after = Int(get(data, "after", 5))
    perBook = Int(get(data, "perBook", 3))
    schema = String(get(data, "schema", CONFIG.default_schema))
    useFilter = Bool(get(data, "useFilter", false))
    filterIds = Vector{Int}(get(data, "filterIds", Int[]))

    combined = Dict{String, Int}()
    found_any = false
    for path in CONFIG.postings_dbs
        db = connect_postings(path, CONFIG.ext_path)
        wdb = connect_words(shard_words_path(path))
        if useFilter && !isempty(filterIds)
            ensure_urn_filter(db, filterIds)
        end
        cf_id = get_cf_id(wdb, word)
        if cf_id === nothing
            SQLite.close(db)
            SQLite.close(wdb)
            continue
        end
        found_any = true
        counts = sample_collocations(
            db,
            wdb,
            cf_id,
            perBook,
            before,
            after,
            useFilter && !isempty(filterIds),
            schema,
        )
        for (w, c) in counts
            combined[w] = get(combined, w, 0) + c
        end
        SQLite.close(db)
        SQLite.close(wdb)
    end
    if !found_any
        return json_response(Dict("error" => "Word not found"), 404)
    end
    pairs = collect(combined)
    sort!(pairs, by = x -> -x[2])
    top = pairs[1:min(50, length(pairs))]
    rows = [Dict("word" => p[1], "count" => p[2]) for p in top]
    return json_response(Dict("rows" => rows))
end


function handle_health(_req::HTTP.Request)
    return json_response(Dict("status" => "ok", "version" => "0.1.0"))
end


const ROUTER = HTTP.Router()
HTTP.register!(ROUTER, "GET", "/health", handle_health)
HTTP.register!(ROUTER, "POST", "/concordance", handle_concordance)
HTTP.register!(ROUTER, "POST", "/near_frequency", handle_near_frequency)
HTTP.register!(ROUTER, "POST", "/collocations", handle_collocations)


function run()
    host = get(ENV, "HOST", "0.0.0.0")
    port = parse(Int, get(ENV, "PORT", "8001"))
    HTTP.serve(ROUTER, host, port)
end


run()

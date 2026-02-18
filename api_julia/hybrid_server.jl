using HTTP
using JSON3
using Random

include("sqlite_blob_julia_probe.jl")

const CFG = load_config()

function json_response(obj, status::Int=200)
    return HTTP.Response(
        status,
        JSON3.write(obj),
        [
            "Content-Type" => "application/json",
            "Access-Control-Allow-Origin" => "*",
            "Access-Control-Allow-Methods" => "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers" => "*",
        ],
    )
end

function parse_body(req::HTTP.Request)
    body = String(req.body)
    return isempty(body) ? Dict() : JSON3.read(body)
end

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

function as_int(x, default::Int)
    try
        return Int(x)
    catch
        return default
    end
end

function as_bool(x, default::Bool)
    try
        return Bool(x)
    catch
        return default
    end
end

function run_payload_once(payload)
    rng = MersenneTwister(rand(UInt))
    return run_once(payload, CFG, rng)
end

function validate_near_payload(data)
    has_groups = haskey(data, :termGroups) && nonempty_json_array(data.termGroups)
    has_terms = haskey(data, :terms) && nonempty_json_array(data.terms)
    if has_groups
        if length(data.termGroups) < 2
            return false, "termGroups must contain at least two items"
        end
        return true, ""
    end
    if has_terms
        if length(data.terms) < 2
            return false, "terms must contain at least two items"
        end
        return true, ""
    end
    return false, "terms must contain at least two items"
end

function handle_health(_req::HTTP.Request)
    return json_response(Dict("status" => "ok", "version" => "0.1.0-julia-hybrid"))
end

function handle_near_query(req::HTTP.Request)
    data = parse_body(req)
    ok, msg = validate_near_payload(data)
    if !ok
        return json_response(Dict("detail" => msg), 400)
    end
    payload = Dict(
        "terms" => get(data, :terms, nothing),
        "termGroups" => get(data, :termGroups, nothing),
        "window" => as_int(get(data, :window, 5), 5),
        "symmetric" => as_bool(get(data, :symmetric, true), true),
        "maxVariants" => as_int(get(data, :maxVariants, 10), 10),
        "docSamples" => 0,
        "perBook" => 0,
        "totalLimit" => 0,
        "useFilter" => as_bool(get(data, :useFilter, false), false),
        "filterIds" => get(data, :filterIds, Int[]),
        "parallelShards" => as_bool(get(data, :parallelShards, false), false),
        "mode" => "count",
    )
    try
        out = run_payload_once(payload)
        return json_response(
            Dict(
                "total" => Int(get(out, "total", 0)),
                "docs" => Int(get(out, "docs", 0)),
                "_engine" => "julia",
                "_perf" => get(out, "timings_ms", Dict()),
            )
        )
    catch e
        return json_response(Dict("detail" => "Julia near_query failed: $(e)"), 500)
    end
end

function handle_near_fragments(req::HTTP.Request)
    data = parse_body(req)
    ok, msg = validate_near_payload(data)
    if !ok
        return json_response(Dict("detail" => msg), 400)
    end
    include_fragments = as_bool(get(data, :includeFragments, true), true)
    mode = include_fragments ? "fragments" : "hits"
    payload = Dict(
        "terms" => get(data, :terms, nothing),
        "termGroups" => get(data, :termGroups, nothing),
        "window" => as_int(get(data, :window, 5), 5),
        "before" => as_int(get(data, :before, 5), 5),
        "after" => as_int(get(data, :after, 5), 5),
        "perBook" => as_int(get(data, :perBook, 3), 3),
        "docSamples" => as_int(get(data, :docSamples, 0), 0),
        "totalLimit" => as_int(get(data, :totalLimit, 200), 200),
        "symmetric" => as_bool(get(data, :symmetric, true), true),
        "maxVariants" => as_int(get(data, :maxVariants, 10), 10),
        "useFilter" => as_bool(get(data, :useFilter, false), false),
        "filterIds" => get(data, :filterIds, Int[]),
        "parallelShards" => as_bool(get(data, :parallelShards, false), false),
        "mode" => mode,
    )
    try
        out = run_payload_once(payload)
        raw_rows = get(out, "rows", Any[])
        rows = Vector{Dict{String, Any}}()
        for r in raw_rows
            if !haskey(r, "bookId")
                continue
            end
            pos = haskey(r, "seq") ? Int(r["seq"]) : Int(get(r, "pos", 0))
            if include_fragments
                push!(
                    rows,
                    Dict(
                        "bookId" => Int(r["bookId"]),
                        "pos" => pos,
                        "frag" => String(get(r, "frag", "")),
                    ),
                )
            else
                push!(rows, Dict("bookId" => Int(r["bookId"]), "pos" => pos, "frag" => ""))
            end
        end
        if isempty(rows)
            return json_response(Dict("detail" => "No near fragments found"), 404)
        end
        return json_response(
            Dict(
                "rows" => rows,
                "_engine" => "julia",
                "_perf" => get(out, "timings_ms", Dict()),
            )
        )
    catch e
        return json_response(Dict("detail" => "Julia near_fragments failed: $(e)"), 500)
    end
end

function handle_near_hits(req::HTTP.Request)
    data = parse_body(req)
    payload = Dict{String, Any}()
    for (k, v) in pairs(data)
        payload[String(k)] = v
    end
    payload["includeFragments"] = false
    req2 = HTTP.Request("POST", "/near_fragments", ["Content-Type" => "application/json"], JSON3.write(payload))
    return handle_near_fragments(req2)
end

function handle_options(_req::HTTP.Request)
    return HTTP.Response(
        204,
        "",
        [
            "Access-Control-Allow-Origin" => "*",
            "Access-Control-Allow-Methods" => "GET,POST,OPTIONS",
            "Access-Control-Allow-Headers" => "*",
        ],
    )
end

const ROUTER = HTTP.Router()
HTTP.register!(ROUTER, "GET", "/health", handle_health)
HTTP.register!(ROUTER, "POST", "/near_query", handle_near_query)
HTTP.register!(ROUTER, "POST", "/near_fragments", handle_near_fragments)
HTTP.register!(ROUTER, "POST", "/near_hits", handle_near_hits)
HTTP.register!(ROUTER, "OPTIONS", "/near_query", handle_options)
HTTP.register!(ROUTER, "OPTIONS", "/near_fragments", handle_options)
HTTP.register!(ROUTER, "OPTIONS", "/near_hits", handle_options)
HTTP.register!(ROUTER, "OPTIONS", "/health", handle_options)

host = get(ENV, "HOST", "0.0.0.0")
port = parse(Int, get(ENV, "PORT", "8001"))
HTTP.serve(ROUTER, host, port)

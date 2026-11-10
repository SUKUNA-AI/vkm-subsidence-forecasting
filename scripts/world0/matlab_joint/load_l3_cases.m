function data = load_l3_cases(inputFile)
% Prepare independent per-world time grids. Input order is the persistent node order.
manifest = jsondecode(fileread(inputFile));
if isfield(manifest, 'world_paths')
    names = string(manifest.world_paths);
    [folder, ~, ~] = fileparts(inputFile);
    for i = 1:numel(names)
        if ~isfile(names(i))
            names(i) = fullfile(folder, names(i));
        end
    end
else
    names = string(inputFile);
end
assert(numel(names) > 0 && numel(names) <= 64, 'This pilot permits 1 to 64 worlds');
data.source_paths = names;
data.world_ids = strings(1, numel(names));
data.representation_ids = data.world_ids;
numericFields = ["area_m2", "height_m", "young_pa", "viscosity_pa_s", ...
    "chamber_fraction", "fill_stiffness_n_m", "fill_gap_m", "load_n", "initial_memory_m"];
for b = 1:numel(names)
    w = jsondecode(fileread(names(b)));
    assert(strcmp(w.schema, 'vkm.world0.joint/1'));
    nodeIds = string({w.nodes.id})';
    n = numel(nodeIds);
    assert(n > 1 && n <= 20000 && numel(unique(nodeIds)) == n && all(strlength(nodeIds) > 0));
    if b == 1
        data.node_ids = nodeIds;
    else
        assert(isequal(data.node_ids, nodeIds), 'Batch requires common topology and node order');
    end
    data.world_ids(b) = string(w.world_id);
    data.representation_ids(b) = string(w.representation_id);
    for f = numericFields
        values = vertcat(w.nodes.(char(f)));
        assert(isequal(size(values), [n, 1]) && all(isfinite(values)));
        data.(f)(:, b) = values;
    end
    if isempty(w.edges)
        error('This whole-domain pilot requires a nonempty physical graph');
    end
    [aOk, a] = ismember(string({w.edges.a}), nodeIds);
    [bOk, z] = ismember(string({w.edges.b}), nodeIds);
    pairs = sort([a(:), z(:)], 2);
    [pairs, order] = sortrows(pairs);
    assert(all(aOk & bOk) && all(pairs(:, 1) ~= pairs(:, 2)) && ...
        size(unique(pairs, 'rows'), 1) == size(pairs, 1));
    weights = [w.edges.stiffness_n_m]';
    assert(all(isfinite(weights) & weights > 0));
    if b == 1
        data.pairs = pairs;
        m = size(pairs, 1);
        rows = (1:m)';
        data.incidence = sparse([rows; rows], pairs(:), [ones(m, 1); -ones(m, 1)], m, n);
        data.incidence_t = data.incidence';
        data.incidence_abs_t = abs(data.incidence_t);
        assert(max(conncomp(graph(pairs(:, 1), pairs(:, 2), [], n))) == 1, ...
            'Physical graph must span the full input');
    else
        assert(isequal(data.pairs, pairs), 'Batch graph topology differs');
    end
    data.weight(:, b) = weights(order);
    data.schedule{b} = calendar(w, nodeIds);
end
assert(numel(unique(data.world_ids)) == numel(names), 'World identifiers must be unique');
assert(all(data.area_m2 > 0 & data.height_m > 0 & data.young_pa > 0 & ...
    data.viscosity_pa_s > 0 & data.chamber_fraction >= 0 & data.chamber_fraction < 1 & ...
    data.fill_stiffness_n_m >= 0 & data.fill_gap_m >= 0 & data.load_n > 0 & ...
    data.initial_memory_m >= 0, 'all'), 'Unsupported or nonphysical pillar inputs');
data.steps = max(cellfun(@(s) numel(s.time_s), data.schedule));
assert(data.steps <= 512 && 8 * 9 * n * numel(names) * data.steps < 3e9, ...
    'Pilot history exceeds preset memory/step budget');
end

function schedule = calendar(w, nodeIds)
assert(isfinite(w.max_dt_s) && isfinite(w.end_time_s) && w.max_dt_s > 0 && w.end_time_s > 0);
events = w.events;
if isempty(events)
    eventTimes = zeros(0, 1);
    eventNodes = zeros(0, 1);
    kinds = strings(0, 1);
else
    eventTimes = [events.time_s]';
    [nodeOk, eventNodes] = ismember(string({events.node})', nodeIds);
    kinds = string({events.kind})';
    assert(all(nodeOk) && all(isfinite(eventTimes) & eventTimes > 0 & eventTimes <= w.end_time_s));
    assert(all(ismember(kinds, ["excavate", "backfill"])));
    assert(size(unique([eventTimes, eventNodes], 'rows'), 1) == numel(eventNodes));
end
stops = unique([eventTimes; w.end_time_s]);
t = 0;
time = 0;
phase = "initial";
excavate = {zeros(0, 1)};
birth = {zeros(0, 1)};
state = zeros(numel(nodeIds), 1);
for stop = stops'
    while t < stop
        if stop - t <= w.max_dt_s
            t = stop;
        else
            t = t + w.max_dt_s;
        end
        assert(numel(time) < 512, 'Pilot calendar exceeds preset step budget');
        time(end+1) = t;
        phase(end+1) = "step";
        excavate{end+1} = zeros(0, 1);
        birth{end+1} = zeros(0, 1);
    end
    mine = eventNodes(eventTimes == stop & kinds == "excavate");
    fill = eventNodes(eventTimes == stop & kinds == "backfill");
    if isempty(mine) && isempty(fill)
        continue
    end
    assert(all(state(mine) == 0) && all(state(fill) == 1), 'Invalid event lineage');
    state(mine) = 1;
    state(fill) = 2;
    phase(end) = "pre_event";
    time(end+1) = stop;
    phase(end+1) = "post_event";
    excavate{end+1} = mine;
    birth{end+1} = fill;
end
schedule = struct('time_s', time, 'phase', phase, 'excavate', {excavate}, 'birth', {birth});
end

function run_l3(manifest, externalReference, mode)
% Native CPU independent direct solves and coupled GPU FP64 trajectories.
if nargin < 2
    externalReference = '';
end
if nargin < 3
    mode = 'qualification';
end
assert(ismember(string(mode), ["qualification", "ensemble"]));
wall = tic;
loadTimer = tic;
data = load_l3_cases(manifest);
report.input_read_s = toc(loadTimer);
columns = 1:numel(data.world_ids);
last = data.steps;
report.status = 'STARTED';
report.world_ids = data.world_ids;
report.representation_ids = data.representation_ids;
report.sources = data.source_paths;
report.nodes = numel(data.node_ids);
report.edges = size(data.pairs, 1);
report.worlds = numel(columns);
report.batch = numel(columns);
report.steps = cellfun(@(s) numel(s.time_s), data.schedule);
report.mode = mode;
report.precision = 'float64';
report.matlab = version;
report.matlab_release = version('-release');
report.toolboxes = ver;
report.step_force_residual_limit_relative = 2e-9;
report.internal_force_balance_limit_relative = 1e-13;
write_json('started_native.json', report);
[cpu, ~, report.timing_cpu] = solve_l3(data, columns, [], 1, last, []);
[device, report.device] = initialize_device();
assert(device.AvailableMemory > 8 * 18 * report.nodes * report.batch * last + 2e9, ...
    'Insufficient measured GPU memory with reserve');
[gpu, ~, report.timing_gpu] = solve_l3(data, columns, device, 1, last, []);
report.cpu_gpu = compare_outputs(cpu, gpu, data);
report.cpu_max_equilibrium_residual = max(cpu.scaled_equilibrium_residual(cpu.valid));
report.gpu_max_equilibrium_residual = max(gpu.scaled_equilibrium_residual(gpu.valid));
report.gpu_max_internal_force_imbalance = max(gpu.relative_internal_force_balance(gpu.valid));
report.gpu_total_cg_iterations = sum(gpu.iterations(gpu.valid));
report.event_counts = sum(gpu.phase == "post_event", 2);
report.max_active_set_iterations = max(gpu.active_iterations);
report.final_max_convergence_m = zeros(1, report.batch);
report.final_max_memory_m = zeros(1, report.batch);
report.final_active_fill_nodes = zeros(1, report.batch);
for b = columns
    ix = find(gpu.valid(b, :), 1, 'last');
    report.final_max_convergence_m(b) = max(gpu.fields(:, b, ix, 1));
    report.final_max_memory_m(b) = max(gpu.fields(:, b, ix, 2));
    report.final_active_fill_nodes(b) = sum(gpu.fields(:, b, ix, 7));
end
report.external_reference = 'NOT_PROVIDED';
if strlength(externalReference) > 0
    report.external_reference = compare_reference(cpu, data, externalReference);
end
if strcmp(mode, 'qualification')
    if numel(columns) > 1
        order = [columns(2:end), columns(1)];
        [permuted, ~] = solve_l3(data, order, device, 1, last, []);
        report.batch_permutation = compare_outputs(gpu, permuted, data);
        report.batch_split = cell(1, 2);
        split = floor(numel(columns) / 2);
        groups = {columns(1:split), columns(split+1:end)};
        for group = 1:2
            [piece, ~] = solve_l3(data, groups{group}, device, 1, last, []);
            report.batch_split{group} = compare_outputs(gpu, piece, data);
        end
    else
        report.batch_permutation = 'NOT_RUN_ONE_WORLD';
        report.batch_split = 'NOT_RUN_ONE_WORLD';
    end
    [~, checkpoint] = solve_l3(data, columns, device, 1, floor(last / 2), []);
    nextStep = floor(last / 2) + 1;
    save('restart_checkpoint.mat', 'checkpoint', 'nextStep', 'columns', '-v7.3');
    clear checkpoint
    cp = load('restart_checkpoint.mat');
    try
        solve_l3(data, cp.columns, device, cp.nextStep, cp.nextStep, cp.checkpoint, cp.nextStep);
        error('VKM:ControlFailed', 'Injected invalid-force step was accepted');
    catch cause
        assert(strcmp(cause.identifier, 'VKM:RejectedTrial'), '%s', cause.message);
        report.rejected_step = struct('status', 'REJECTED', 'step', cp.nextStep, ...
            'identifier', cause.identifier, 'message', cause.message);
    end
    [restarted, ~] = solve_l3(data, cp.columns, device, cp.nextStep, last, cp.checkpoint);
    report.restart_after_rejection = compare_outputs(gpu, restarted, data, cp.nextStep);
    if any(cp.checkpoint.m ~= 0, 'all')
        cp.checkpoint.m(:) = 0;
        [corrupt, ~] = solve_l3(data, cp.columns, device, cp.nextStep, last, cp.checkpoint);
        change = max(abs(corrupt.fields(:, :, :, 1) - restarted.fields(:, :, :, 1)), [], 'all');
        assert(change > 1e-10, 'Reset-memory negative control was not detected');
        report.reset_memory_negative_control = struct('status', 'DETECTED', ...
            'max_convergence_difference_m', change);
    else
        report.reset_memory_negative_control = 'NOT_APPLICABLE_ZERO_CHECKPOINT_MEMORY';
    end
    report.contact_precision = check_precision(device);
else
    report.qualifications = 'Use separate matching topology qualification batch for permutation, split and restart';
end
node_ids = data.node_ids;
world_ids = data.world_ids;
representation_ids = data.representation_ids;
fields = ["convergence_m", "creep_memory_m", "pillar_force_n", "fill_force_n", ...
    "coupling_force_n", "fill_reference_m", "resolved_contact_mask", "excavation_mask"];
save('trajectory.mat', 'cpu', 'gpu', 'node_ids', 'world_ids', 'representation_ids', 'fields', '-v7.3');
write_summaries(data, gpu);
report.device.available_memory_after_bytes = device.AvailableMemory;
report.wall_s = toc(wall);
report.status = 'READY_FOR_REVIEW';
write_json('result.json', report);
fprintf('READY_FOR_REVIEW, %d nodes, %d edges, %d independent worlds, GPU step residual %.4g\n', ...
    report.nodes, report.edges, report.worlds, report.gpu_max_equilibrium_residual);
end

function metrics = compare_outputs(expected, actual, data, first)
if nargin < 4
    first = 1;
end
expectedSteps = first:(first + size(actual.valid, 2) - 1);
metrics = struct('max_abs_per_field', zeros(1, 8), 'worlds', numel(actual.columns));
for j = 1:numel(actual.columns)
    col = actual.columns(j);
    ix = find(expected.columns == col, 1);
    assert(~isempty(ix) && isequal(expected.valid(ix, expectedSteps), actual.valid(j, :)));
    valid = actual.valid(j, :);
    assert(isequal(expected.phase(ix, expectedSteps(valid)), actual.phase(j, valid)));
    assert(isequal(expected.time_s(ix, expectedSteps(valid)), actual.time_s(j, valid)));
    absoluteFloor = [1e-10, 1e-10, 1e-3, 1e-3, 1e-3, 1e-10, 0, 0];
    loadScale = max(abs(data.load_n(:, col)));
    physicalScale = [1, 1, loadScale, loadScale, loadScale, 1, 0, 0];
    for f = 1:8
        a = expected.fields(:, ix, expectedSteps(valid), f);
        b = actual.fields(:, j, valid, f);
        assert(isequal(isnan(a), isnan(b)), 'Different field support for field %d', f);
        a = a(~isnan(a));
        b = b(~isnan(b));
        if isempty(a)
            continue
        end
        err = max(abs(a - b));
        bound = absoluteFloor(f) + 2e-9 * max(physicalScale(f), max(abs(a)));
        if f >= 7
            bound = 0;
        end
        assert(isfinite(err) && err <= bound, ...
            'Discrepancy in world %d, field %d: %.8g > %.8g', col, f, err, bound);
        metrics.max_abs_per_field(f) = max(metrics.max_abs_per_field(f), err);
    end
end
metrics.status = 'MATCH';
end

function result = compare_reference(cpu, data, path)
r = jsondecode(fileread(path));
col = find(data.world_ids == string(r.world_id), 1);
assert(~isempty(col) && data.representation_ids(col) == string(r.representation_id));
assert(isequal(data.node_ids, string(r.node_ids(:))));
snap = r.snapshots;
if ~iscell(snap)
    snap = num2cell(snap);
end
steps = find(cpu.valid(col, :));
assert(numel(snap) == numel(steps));
names = ["convergence_m", "creep_memory_m", "pillar_force_n", "fill_force_n", ...
    "coupling_force_n", "fill_reference_m"];
errors = zeros(1, numel(names));
loadScale = max(abs(data.load_n(:, col)));
bound = [1e-9, 1e-9, max(1e-3, 2e-9 * loadScale) * ones(1, 3), 1e-9];
for j = steps
    row = snap{j};
    assert(abs(row.time_s - cpu.time_s(col, j)) <= 1e-9 * max(1, row.time_s));
    assert(string(row.phase) == cpu.phase(col, j));
    for k = 1:numel(names)
        a = row.(names(k));
        if iscell(a)
            for ix = 1:numel(a)
                if isempty(a{ix})
                    a{ix} = NaN;
                end
            end
            a = cell2mat(a);
        end
        b = cpu.fields(:, col, j, k);
        a = a(:);
        assert(isequal(isnan(a), isnan(b)));
        mask = ~isnan(a);
        if any(mask)
            errors(k) = max(errors(k), max(abs(a(mask) - b(mask))));
        end
    end
end
assert(all(errors <= bound), 'Independent World-0 trajectory mismatch');
result = struct('status', 'MATCH', 'path', path, 'world_id', r.world_id, ...
    'input_sha256', r.input_sha256, 'fields', names, 'max_abs', errors, 'bound', bound);
end

function result = check_precision(device)
reference = 0.05;
trial = reference + [-2e-10, -2e-12, 0, 2e-12, 2e-10];
expected = (trial - reference) > 1e-12;
actualDouble = gather((gpuArray(trial) - gpuArray(reference)) > 1e-12);
actualSingle = gather((gpuArray(single(trial)) - gpuArray(single(reference))) > single(1e-12));
wait(device);
assert(isequal(actualDouble, expected));
assert(any(actualSingle ~= expected), 'Near-contact FP32 negative case unexpectedly indistinguishable');
result = struct('device', device.Name, 'gap_reference_m', reference, ...
    'trial_displacements_m', trial, 'deadband_m', 1e-12, ...
    'cpu_fp64', expected, 'gpu_fp64', actualDouble, 'gpu_fp32', actualSingle, ...
    'fp32_classification_disagreements', nnz(actualSingle ~= expected), ...
    'interpretation', 'Local threshold control only. Full FP32 trajectories are unqualified.');
end

function write_summaries(data, out)
file = fopen('field_summary.csv', 'w');
assert(file >= 0);
closed = onCleanup(@() fclose(file));
fprintf(file, 'world_id,time_s,phase,max_convergence_m,max_creep_memory_m,total_fill_force_n,max_abs_coupling_force_n,active_fill_nodes,force_residual_rel\n');
for b = 1:numel(data.world_ids)
    for j = find(out.valid(b, :))
        f = out.fields(:, b, j, :);
        fprintf(file, '%s,%.17g,%s,%.17g,%.17g,%.17g,%.17g,%d,%.17g\n', data.world_ids(b), ...
            out.time_s(b, j), out.phase(b, j), max(f(:, :, :, 1)), max(f(:, :, :, 2)), ...
            sum(f(:, :, :, 4)), max(abs(f(:, :, :, 5))), sum(f(:, :, :, 7)), ...
            out.scaled_equilibrium_residual(b, j));
    end
end
end

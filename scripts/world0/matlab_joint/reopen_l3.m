function reopen_l3(attempt, manifest, output)
% CPU-only verification of persisted native results, independent of the GPU runner.
data = load_l3_cases(manifest);
native = load(fullfile(attempt, 'trajectory.mat'));
assert(isequal(native.node_ids, data.node_ids) && isequal(native.world_ids, data.world_ids));
assert(isequal(native.representation_ids, data.representation_ids));
assert(isequal(native.cpu.valid, native.gpu.valid));
assert(isequaln(native.cpu.time_s, native.gpu.time_s));
assert(isequal(native.cpu.phase, native.gpu.phase));
status = jsondecode(fileread(fullfile(attempt, 'result.json')));
assert(strcmp(status.status, 'READY_FOR_REVIEW') && strcmp(status.precision, 'float64'));
assert(strcmp(status.cpu_gpu.status, 'MATCH') && status.device.supports_double);
fid = fopen(fullfile(output, 'final_fields.csv'), 'w');
assert(fid >= 0);
cleanup = onCleanup(@() fclose(fid));
fprintf(fid, 'world_id,representation_id,node_id,time_s,convergence_m,creep_memory_m,pillar_force_n,fill_force_n,coupling_force_n,fill_reference_m,contact,excavated\n');
result = struct('status', 'REOPENED_AND_EQUATIONS_MATCH', 'matlab', version, ...
    'worlds', numel(data.world_ids), 'nodes', numel(data.node_ids), ...
    'steps_checked', 0, 'events_checked', 0, 'max_equilibrium_rel', 0, ...
    'max_constitutive_force_rel', 0, 'max_coupling_force_rel', 0, ...
    'max_memory_increment_error_m', 0, 'max_fill_birth_reference_error_m', 0);
for b = 1:numel(data.world_ids)
    jList = find(native.gpu.valid(b, :));
    schedule = data.schedule{b};
    assert(isequal(native.gpu.time_s(b, jList), schedule.time_s));
    assert(isequal(native.gpu.phase(b, jList), schedule.phase));
    a0 = data.area_m2(:, b);
    eta = data.viscosity_pa_s(:, b);
    h = data.height_m(:, b);
    E = data.young_pa(:, b);
    f = data.chamber_fraction(:, b);
    fN = data.load_n(:, b);
    scale = max(abs(fN));
    edge = data.incidence;
    w = data.weight(:, b);
    fields = native.gpu.fields(:, b, jList, :);
    oldTime = 0;
    oldMemory = data.initial_memory_m(:, b);
    oldQ = nan(size(oldMemory));
    for j = 1:numel(jList)
        q = fields(:, 1, j, 1);
        memory = fields(:, 1, j, 2);
        pillarN = fields(:, 1, j, 3);
        fillN = fields(:, 1, j, 4);
        jointN = fields(:, 1, j, 5);
        reference = fields(:, 1, j, 6);
        mined = logical(fields(:, 1, j, 8));
        t = schedule.time_s(j);
        area = a0 .* (1 - f .* mined);
        expectedPillar = E .* area ./ h .* (q - memory);
        expectedFill = zeros(size(q));
        activeFill = ~isnan(reference);
        expectedFill(activeFill) = data.fill_stiffness_n_m(activeFill, b) .* ...
            max(q(activeFill) - reference(activeFill), 0);
        expectedJoint = edge' * (w .* (edge * q));
        memoryDelta = (t - oldTime) .* h ./ (eta .* area) .* pillarN;
        result.max_equilibrium_rel = max(result.max_equilibrium_rel, max(abs(pillarN + fillN + jointN - fN)) / scale);
        result.max_constitutive_force_rel = max(result.max_constitutive_force_rel, ...
            max(abs([expectedPillar - pillarN; expectedFill - fillN])) / scale);
        result.max_coupling_force_rel = max(result.max_coupling_force_rel, max(abs(expectedJoint - jointN)) / scale);
        result.max_memory_increment_error_m = max(result.max_memory_increment_error_m, ...
            max(abs(memory - oldMemory - memoryDelta)));
        if schedule.phase(j) == "post_event"
            assert(t == oldTime && isequal(memory, oldMemory));
            born = schedule.birth{j};
            if ~isempty(born)
                e = max(abs(reference(born) - oldQ(born) - data.fill_gap_m(born, b)));
                result.max_fill_birth_reference_error_m = max(result.max_fill_birth_reference_error_m, e);
            end
            assert(all(mined(schedule.excavate{j})));
            result.events_checked = result.events_checked + numel(born) + numel(schedule.excavate{j});
        end
        oldTime = t;
        oldMemory = memory;
        oldQ = q;
        result.steps_checked = result.steps_checked + 1;
    end
    final = squeeze(fields(:, 1, end, :));
    for j = 1:numel(data.node_ids)
        fprintf(fid, '%s,%s,%s,%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,%.17g,%d,%d\n', ...
            data.world_ids(b), data.representation_ids(b), data.node_ids(j), oldTime, final(j, :));
    end
end
assert(result.max_equilibrium_rel < 2e-9 && result.max_constitutive_force_rel < 2e-9 && ...
    result.max_coupling_force_rel < 2e-9 && result.max_memory_increment_error_m < 1e-10 && ...
    result.max_fill_birth_reference_error_m < 1e-10);
write_json(fullfile(output, 'reopen.json'), result);
disp(result);
end

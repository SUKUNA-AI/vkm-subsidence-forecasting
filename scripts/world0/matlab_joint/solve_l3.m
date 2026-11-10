function [out, saved, timing] = solve_l3(data, columns, device, first, last, saved, injectedFailureStep)
% One independent world per column. A distinct clock and event lineage are
% retained for each column. The full physical graph is used at each step.
gpu = ~isempty(device);
if nargin < 7
    injectedFailureStep = -1;
end
if gpu
    put = @gpuArray;
    get = @gather;
else
    put = @(x) x;
    get = put;
end
n = numel(data.node_ids);
b = numel(columns);
s = last - first + 1;
if gpu
    wait(device);
end
transfer = tic;
E = put(data.young_pa(:, columns));
eta = put(data.viscosity_pa_s(:, columns));
area = put(data.area_m2(:, columns));
height = put(data.height_m(:, columns));
fraction = put(data.chamber_fraction(:, columns));
fillK = put(data.fill_stiffness_n_m(:, columns));
gap = put(data.fill_gap_m(:, columns));
loadN = put(data.load_n(:, columns));
weight = put(data.weight(:, columns));
edge = put(data.incidence);
edgeT = put(data.incidence_t);
edgeAbsT = put(data.incidence_abs_t);
if isempty(saved)
    assert(first == 1);
    saved = struct('q', zeros(n, b), 'm', data.initial_memory_m(:, columns), ...
        'fill_reference', zeros(n, b), 'has_fill', false(n, b), ...
        'excavated', false(n, b), 'time_s', zeros(1, b));
end
assert(isequal(size(saved.q), [n, b]));
q = put(saved.q);
memory = put(saved.m);
reference = put(saved.fill_reference);
hasFill = saved.has_fill;
mined = saved.excavated;
lastTime = saved.time_s;
if gpu
    wait(device);
end
timing.host_to_device_s = toc(transfer);

clock = tic;
history = zeros(n, b, s, 8, 'like', q);
valid = false(b, s);
calendar = nan(b, s);
phase = strings(b, s);
iterations = zeros(b, s);
residual = zeros(b, s);
graphNet = zeros(b, s);
diss = zeros(b, s);
activeIterations = zeros(1, s);
loadScale = max(abs(loadN), [], 1);
edgeDiagonal = full(edgeAbsT * weight);
if ~gpu
    cpuL = cell(1, b);
    for j = 1:b
        cpuL{j} = edgeT * spdiags(weight(:, j), 0, size(edge, 1), size(edge, 1)) * edge;
    end
end
for step = first:last
    k = step - first + 1;
    t = lastTime;
    bornNow = false(n, b);
    for j = 1:b
        schedule = data.schedule{columns(j)};
        if step > numel(schedule.time_s)
            continue
        end
        valid(j, k) = true;
        t(j) = schedule.time_s(step);
        calendar(j, k) = t(j);
        phase(j, k) = schedule.phase(step);
        mined(schedule.excavate{step}, j) = true;
        bornNow(schedule.birth{step}, j) = true;
    end
    hasFill = hasFill | bornNow;
    bornNowDevice = put(bornNow);
    reference(bornNowDevice) = q(bornNowDevice) + gap(bornNowDevice);
    dt = put(t - lastTime);
    assert(all(t >= lastTime), 'Calendar moved backwards');
    retained = area .* (1 - fraction .* put(mined));
    mobility = height ./ (eta .* retained);
    kEff = E .* retained ./ height ./ (1 + dt .* E ./ eta);
    hasFillDevice = put(hasFill);
    trialQ = q;
    loadWithMemory = loadN + kEff .* memory;
    active = hasFillDevice & trialQ > reference;
    work = zeros(1, b);
    for activeIter = 1:32
        contact = fillK .* active;
        diagK = kEff + contact;
        rhs = loadWithMemory + contact .* reference;
        if gpu
            [trialQ, it] = batched_cg(edge, edgeT, weight, diagK, edgeDiagonal, rhs, trialQ, get);
        else
            for j = 1:b
                trialQ(:, j) = (cpuL{j} + spdiags(diagK(:, j), 0, n, n)) \ rhs(:, j);
            end
            it = ones(1, b);
        end
        work = work + it;
        difference = trialQ - reference;
        % Only suppress numerically ambiguous sign changes for the active-set
        % iteration. The final unilateral force uses the exact positive part.
        nextActive = hasFillDevice & ((difference > 1e-12) | (abs(difference) <= 1e-12 & active));
        if get(all(nextActive == active, 'all'))
            break
        end
        active = nextActive;
    end
    assert(activeIter < 32, 'Unilateral contact active-set iteration did not converge');
    pillarN = kEff .* (trialQ - memory);
    deltaM = dt .* mobility .* pillarN;
    trialM = memory + deltaM;
    fillN = hasFillDevice .* fillK .* max(0, trialQ - reference);
    graphN = full(edgeT * (weight .* full(edge * trialQ)));
    if step == injectedFailureStep
        graphN(1, 1) = graphN(1, 1) + loadN(1, 1);
    end
    res = get(max(abs(pillarN + fillN + graphN - loadN), [], 1) ./ loadScale);
    net = get(abs(sum(graphN, 1)) ./ sum(abs(loadN), 1));
    stepDiss = get(sum(pillarN .* deltaM, 1));
    if ~all(isfinite(res) & res <= 2e-9) || ~all(net <= 1e-13) || ~all(stepDiss >= 0)
        error('VKM:RejectedTrial', 'Trial equilibrium rejected. Residual=%g, internal balance=%g', max(res), max(net));
    end
    assert(get(all(isfinite(trialM) & isfinite(trialQ), 'all')), 'Nonfinite trial state');
    assert(get(all(deltaM(:, (t - lastTime) == 0) == 0, 'all')), 'Instantaneous event changed memory');
    q = trialQ;
    memory = trialM;
    lastTime = t;
    iterations(:, k) = work';
    activeIterations(k) = activeIter;
    residual(:, k) = res';
    graphNet(:, k) = net';
    diss(:, k) = stepDiss';
    refOut = reference;
    refOut(~hasFillDevice) = NaN;
    history(:, :, k, 1) = q;
    history(:, :, k, 2) = memory;
    history(:, :, k, 3) = pillarN;
    history(:, :, k, 4) = fillN;
    history(:, :, k, 5) = graphN;
    history(:, :, k, 6) = refOut;
    history(:, :, k, 7) = hasFillDevice & (q - reference > 1e-12);
    history(:, :, k, 8) = mined;
end
if gpu
    wait(device);
end
timing.compute_and_step_checks_s = toc(clock);
transfer = tic;
history = get(history);
saved = struct('q', get(q), 'm', get(memory), 'fill_reference', get(reference), ...
    'has_fill', hasFill, 'excavated', mined, 'time_s', lastTime);
if gpu
    wait(device);
end
timing.device_to_host_s = toc(transfer);
out = struct('fields', history, 'valid', valid, 'time_s', calendar, 'phase', phase, ...
    'columns', columns, 'iterations', iterations, 'scaled_equilibrium_residual', residual, ...
    'relative_internal_force_balance', graphNet, 'creep_dissipation_J', diss, ...
    'active_iterations', activeIterations);
end

function [x, iterations] = batched_cg(edge, edgeT, weight, diagonal, edgeDiagonal, rhs, x, get)
apply = @(u) full(edgeT * (weight .* full(edge * u))) + diagonal .* u;
inverseDiagonal = 1 ./ (diagonal + edgeDiagonal);
threshold = 1e-12 .* max(abs(rhs), [], 1);
r = rhs - apply(x);
z = inverseDiagonal .* r;
p = z;
rz = sum(r .* z, 1);
iterations = zeros(1, size(rhs, 2), 'like', rhs);
working = max(abs(r), [], 1) > threshold;
for k = 1:400
    if ~get(any(working, 'all'))
        break
    end
    p = p .* working;
    Ap = apply(p);
    denominator = sum(p .* Ap, 1);
    denominator(~working) = 1;
    alpha = rz ./ denominator .* working;
    x = x + alpha .* p;
    r = r - alpha .* Ap;
    iterations = iterations + working;
    working = max(abs(r), [], 1) > threshold;
    z = inverseDiagonal .* r;
    next = sum(r .* z, 1);
    rz(~working) = 1;
    beta = next ./ rz .* working;
    p = z + beta .* p;
    rz = next;
end
assert(~get(any(working, 'all')), 'Batched GPU CG exceeded the preset iteration cap');
iterations = get(iterations);
end

function P = typical_profile(x, x1, x2, H, eta, curve, delta0, psi, edition, strainMethod, Bav)
%VKM.SUBS.TYPICAL_PROFILE Профиль главного сечения по методу типовых кривых ВКМ (конец процесса сдвижения).
%   P = vkm.subs.typical_profile(x, x1, x2, H, eta, curve, delta0, psi, edition, strainMethod, Bav)
%   x       — точки профиля, м; [x1, x2] — панель (2D, бесконечна поперёк сечения); H — глубина разработки, м;
%   eta     — максимальное оседание, м; curve — имя таблицы из vkm.subs.curves();
%   delta0, psi — граничный угол и угол полных сдвижений, градусы;
%   edition = 'current' («Указания» по VKM-SRC-065: (1.16)-(1.18), (1.20), q по (1.15) при l0 = 0,1L)
%           | '1992'    (VKM-SRC-037 (8.138)-(8.142), без q; в (8.138) напечатано L^2 — принято /L, FC-042);
%   strainMethod = 'me' (eps = m_e*K*L, (1.20)) | 'F' (u = eta*F, eps = eta*F'/L, столбцы «Итого» табл. 8.26)
%                | 'avershin' (u = B*i, eps = B*K; B = Bav, VKM-SRC-208 с. 66-67).
%   Знаки: s > 0 вниз; i = ds/dx; K = d2s/dx2 (> 0 — растяжение); u > 0 на восток; eps = du/dx.
%   Между узлами таблицы — линейная интерполяция (MODEL_CHOICE); экстремумы табл. 8.26, отмеченные звёздочкой
%   (поле extremes), — дополнительные узлы S′ и S″. При ширине панели < 2H*ctg(psi) — неполная
%   подработка: максимум в центре, L = H*ctg(delta0) + D/2 (MODEL_CHOICE по VKM-SRC-003 (4.13), VKM-SRC-065 с. 62).
%   Повторяет vkm_world.physics.subsidence_formulas.typical_curve_profile.
if nargin < 9 || isempty(edition), edition = 'current'; end
if nargin < 10 || isempty(strainMethod), strainMethod = 'me'; end
if nargin < 11, Bav = []; end
x = x(:);
tabs = vkm.subs.curves();
tab = tabs.(curve);
D = x2 - x1;
assert(D > 0, 'vkm:subs:panel', 'x2 must exceed x1');
inner = H * cotd(psi);
if D >= 2 * inner
    L = vkm.subs.amplitude('L', H, delta0, psi);
    xl = x1 + inner; xr = x2 - inner; mode = 'full';
else
    L = H * cotd(delta0) + 0.5 * D;
    xl = 0.5 * (x1 + x2); xr = xl; mode = 'incomplete';
end
z = zeros(size(x)); side = zeros(size(x));
left = x < xl; right = x > xr;
z(left) = (xl - x(left)) / L; z(right) = (x(right) - xr) / L;
side(left) = 1; side(right) = -1;
z = min(max(z, 0), 1);
zt = tab.z(:);
S = interp1(zt, tab.S(:), z, 'linear');
if isfield(tab, 'S1_used'), s1 = tab.S1_used(:); else, s1 = tab.S1(:); end
[z1, v1] = with_extremes(tab, 'S1', zt, s1);
[z2, v2] = with_extremes(tab, 'S2', zt, tab.S2(:));
S1 = interp1(z1, v1, z, 'linear');
S2 = interp1(z2, v2, z, 'linear');
flank = side ~= 0;
switch edition
    case 'current'
        l0 = 0.1 * L;
        qi = vkm.subs.amplitude('q', l0, 'tilt'); qk = vkm.subs.amplitude('q', l0, 'curvature');
    case '1992'
        qi = 1; qk = 1;
    otherwise
        error('vkm:subs:edition', 'edition must be current or 1992');
end
s = eta * S;
tilt = flank .* side .* eta .* S1 * qi / L;
K = flank .* eta .* S2 * qk / L^2;
switch strainMethod
    case 'me'
        eps = vkm.subs.amplitude('me', K) .* K * L;
        u = nan(size(x));
    case 'F'
        it = tabs.VKM1992_T8_26_ITOGO;
        u = flank .* side .* eta .* interp1(it.z(:), it.F(:), z, 'linear');
        eps = flank .* eta .* interp1(it.z(:), it.F1(:), z, 'linear') / L;
    case 'avershin'
        assert(~isempty(Bav), 'vkm:subs:B', 'Bav required');
        u = Bav * tilt; eps = Bav * K;
    otherwise
        error('vkm:subs:strain', 'strainMethod must be me, F or avershin');
end
P = struct('x', x, 's', s, 'tilt', tilt, 'curvature', K, 'u_h', u, 'eps_h', eps, ...
    'L', L, 'mode', mode, 'q_tilt', qi, 'q_curvature', qk, 'curve', curve, 'edition', edition);
end

function [zk, vk] = with_extremes(tab, key, zt, vt)
%WITH_EXTREMES Экстремумы, отмеченные в таблице звёздочкой (поле extremes: S′ = 4,0 и 5,5 при Z = 0,35,
%   S″ = +26,0 при Z = 0,45 табл. 8.26 VKM-SRC-037), добавляются к узлам интерполяции. Как в Python _interp.
zk = zt(:); vk = vt(:);
if isfield(tab, 'extremes') && isstruct(tab.extremes) && isfield(tab.extremes, key)
    e = tab.extremes.(key);
    zk = [zk; e(1)]; vk = [vk; e(2)];
    [zk, order] = sort(zk);
    vk = vk(order);
end
end

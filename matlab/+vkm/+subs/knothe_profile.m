function P = knothe_profile(x, x1, x2, H, wmax, beta, Bav)
%VKM.SUBS.KNOTHE_PROFILE Функция влияния Кноте / Литвинишина (гауссова кривая) для 2D-панели [x1, x2].
%   W(x) = Wmax/r * int exp(-pi (x - xi)^2 / r^2) dxi по выработке, r = H/tg(beta) — VKM-SRC-208 с. 67
%   (учебник, GENERAL_METHOD, угольные месторождения; для ВКМ — ANALOGUE). Интеграл по панели:
%   W = Wmax/2 [erf(sqrt(pi)(x - x1)/r) - erf(sqrt(pi)(x - x2)/r)] (DERIVATION); производные аналитические.
%   Горизонтальные сдвижения — по гипотезе Авершина u = B*i (Bav, м), иначе NaN.
%   Повторяет vkm_world.physics.subsidence_formulas.knothe_profile.
if nargin < 7, Bav = []; end
x = x(:);
r = H / tand(beta);
a1 = (x - x1) / r; a2 = (x - x2) / r;
s = 0.5 * wmax * (erf(sqrt(pi) * a1) - erf(sqrt(pi) * a2));
g1 = exp(-pi * a1.^2); g2 = exp(-pi * a2.^2);
tilt = wmax / r * (g1 - g2);
K = wmax / r^2 * (-2 * pi) * (a1 .* g1 - a2 .* g2);
if isempty(Bav)
    u = nan(size(x)); eps = nan(size(x));
else
    u = Bav * tilt; eps = Bav * K;
end
P = struct('x', x, 's', s, 'tilt', tilt, 'curvature', K, 'u_h', u, 'eps_h', eps, 'r', r);
end

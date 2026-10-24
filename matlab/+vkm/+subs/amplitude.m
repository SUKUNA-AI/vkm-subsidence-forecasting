function out = amplitude(name, varargin)
%VKM.SUBS.AMPLITUDE Амплитудные формулы нормативной методики ВКМ (максимальное оседание и коэффициенты).
%   out = vkm.subs.amplitude('backfill', A, B)        p = 1 - A(1 - B)  (VKM-SRC-065 с. 29; VKM-SRC-037 (8.136))
%   out = vkm.subs.amplitude('k', C [, seam])         k: 0,9 при C >= 0,32; 0,9(C/0,32)^2 при C < 0,32; 1,0 карналлит
%                                                     (VKM-SRC-065 с. 29; VKM-SRC-012 (2.1)-(2.2) с. 54)
%   out = vkm.subs.amplitude('eta_current', m0, omega, p, k)   eta = m0*omega*p*k  (1.1) VKM-SRC-065 с. 29
%   out = vkm.subs.amplitude('eta_1992', Ssum, a, b, A, B)     eta0 = 0,9*Ssum/(a+b)*[1 - A(1 - B)]
%                                                     (8.135)-(8.136) VKM-SRC-037 печ. с. 337-338
%   out = vkm.subs.amplitude('chi', D1, D2, H)        chi = 1,12*sqrt((D1-0,15H)(D2-0,15H))/H, D2 -> 1,4H при
%                                                     D1 < 1,4H < D2  (1.10) VKM-SRC-065 с. 30
%   out = vkm.subs.amplitude('f0', chi)               f0 = 0,5948(3,7694(1,6708 chi^0,25 - 1) - chi)  (1.9)
%   out = vkm.subs.amplitude('q', l0, kind)           q = 1 + beta*sqrt(l0/15 - 1)  (1.15) VKM-SRC-065 с. 32
%   out = vkm.subs.amplitude('me', K)                 m_e к (1.20) VKM-SRC-065 с. 33
%   out = vkm.subs.amplitude('L', H, delta0, psi)     L = H(ctg delta0 + ctg psi), углы в градусах
%   out = vkm.subs.amplitude('vzt', H, h, S2max, m, omega, A, B [, epsLim])  предельная мощность слоя соли ВЗТ
%                                                     (8.144) VKM-SRC-037 печ. с. 341, [eps] = 0,002 по умолчанию
%   out = vkm.subs.amplitude('time', t, C, variant)   (1.2)-(1.7) VKM-SRC-065 с. 29-30; t — годы (та же форма
%                                                     в VKM-SRC-103 с. 26 (1.12): «t – ... лет»); ветвь C >= 0,32 —
%                                                     только справочно; variant 'as_printed' | 'beta15_plus'
%   Формулы — нормативная эмпирика (DESIGN), не наблюдения. Реализация повторяет Python-модуль
%   vkm_world.physics.subsidence_formulas; сверка двух реализаций — в matlab/tests и scripts/formulas_vs_fem.py.
switch name
    case 'backfill'
        [A, B] = varargin{:};
        assert(A >= 0 && A <= 1 && B >= 0 && B <= 1, 'vkm:subs:range', 'A, B in [0,1]');
        out = 1 - A * (1 - B);
    case 'k'
        C = varargin{1};
        seam = 'sylvinite';
        if numel(varargin) > 1, seam = varargin{2}; end
        if strcmp(seam, 'carnallite')
            out = 1.0;
        elseif C >= 0.32
            out = 0.9;
        else
            out = 0.9 * (C / 0.32)^2;
        end
    case 'eta_current'
        [m0, omega, p, k] = varargin{:};
        out = m0 * omega * p * k;
    case 'eta_1992'
        [Ssum, a, b, A, B] = varargin{:};
        out = 0.9 * Ssum / (a + b) * (1 - A * (1 - B));
    case 'chi'
        [D1, D2, H] = varargin{:};
        d = sort([D1, D2]);
        if d(1) < 1.4 * H && d(2) > 1.4 * H, d(2) = 1.4 * H; end
        a = d(1) - 0.15 * H; b = d(2) - 0.15 * H;
        if a <= 0 || b <= 0
            out = 0;
        else
            out = 1.12 * sqrt(a * b) / H;
        end
    case 'f0'
        chi = varargin{1};
        if chi <= 0.15
            out = 0;
        elseif chi >= 1.4
            out = 1;
        else
            out = 0.5948 * (3.7694 * (1.6708 * chi^0.25 - 1) - chi);
        end
    case 'q'
        [l0, kind] = varargin{:};
        betas = struct('tilt', 0.1414, 'curvature', 0.1768, 'strain', 0.3535);
        tops = struct('tilt', 1.20, 'curvature', 1.25, 'strain', 1.50);
        if l0 <= 15
            out = 1;
        elseif l0 >= 45
            out = tops.(kind);
        else
            out = 1 + betas.(kind) * sqrt(l0 / 15 - 1);
        end
    case 'me'
        k4 = abs(varargin{1}) * 1e4;
        out = 0.05 + (k4 < 1) .* 0.09 .* max(1 - k4, 0).^3.8;
    case 'L'
        [H, delta0, psi] = varargin{:};
        out = H * (cotd(delta0) + cotd(psi));
    case 'vzt'
        % [m_cj] = 2[eps](0,9H + 0,65h)^2/(S''max*m*omega*(1 - A + A*B))  (8.144) VKM-SRC-037 печ. с. 341
        [H, h, s2max, m, omega, A, B] = varargin{1:7};
        epsLim = 0.002;
        if numel(varargin) > 7, epsLim = varargin{8}; end
        out = 2 * epsLim * (0.9 * H + 0.65 * h)^2 / (s2max * m * omega * (1 - A + A * B));
    case 'time'
        t = varargin{1}; C = varargin{2};
        variant = 'as_printed';
        if numel(varargin) > 2, variant = varargin{3}; end
        if C < 0.32
            out = 0.002 * t;
            return
        elseif C <= 0.40
            alpha = 1.0 - 11.9509 * (C - 0.32)^1.6803;
            s = -1; if strcmp(variant, 'beta15_plus'), s = 1; end
            beta = 0.4 + s * 2.4616 * (C - 0.32)^0.6706;
        elseif C < 0.70
            alpha = 0.8285 - 1.0440 * (C - 0.40)^0.5122;
            beta = 0.8525 - 4.2500 * (C - 0.40);
        else
            error('vkm:subs:range', 'C >= 0.70 is outside (1.4)-(1.7)');
        end
        out = beta * (0.01 * t).^alpha;
    otherwise
        error('vkm:subs:name', 'unknown formula %s', name);
end
end

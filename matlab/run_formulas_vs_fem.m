function run_formulas_vs_fem(scenarioJson, outCsv)
%RUN_FORMULAS_VS_FEM Нормативные профили для сценариев 2D-расчёта MAPDL (панель КрII 600 м) — MATLAB-реализация.
%   run_formulas_vs_fem('<out>/scenario_for_matlab.json', '<out>/formula_profiles_matlab.csv')
%   Сценарий (сетка x, панель, H, m0, omega, углы) пишет scripts/formulas_vs_fem.py; он же сверяет этот CSV
%   с Python-реализацией (поле matlab_check в summary.json). Числа — нормативный расчёт, не наблюдения.
%   Запуск: matlab -batch "maxNumCompThreads(4); addpath('matlab'); run_formulas_vs_fem(...)"
sc = jsondecode(fileread(scenarioJson));
x = sc.x(:); p0 = sc.panel(1); p1 = sc.panel(2); H = sc.H; m0 = sc.m0;
A = @vkm.subs.amplitude;
fid = fopen(outCsv, 'w');
cleaner = onCleanup(@() fclose(fid));
fprintf(fid, 'case,x,s,tilt,curvature,u_h,eps_h\n');
tags = {'w378', sc.omega_A; 'w510', sc.omega_B};
for k = 1:2
    omega = tags{k, 2};
    etaCur = A('eta_current', m0, omega, 1, 0.9);
    eta92 = A('eta_1992', m0 * omega, omega, 1 - omega, 0, 0);
    P = {['UKAZ_T4_' tags{k, 1}], vkm.subs.typical_profile(x, p0, p1, H, etaCur, 'VKM_UKAZANIYA_T4_SRC065', ...
            sc.delta_cur, sc.psi_cur, 'current', 'me')
         ['N1992_MODE2_' tags{k, 1}], vkm.subs.typical_profile(x, p0, p1, H, eta92, 'VKM1992_T8_26_MODE2', ...
            sc.delta_92, sc.psi_92, '1992', 'F')
         ['N1992_MODE3_' tags{k, 1}], vkm.subs.typical_profile(x, p0, p1, H, eta92, 'VKM1992_T8_26_MODE3', ...
            sc.delta_92, sc.psi_92, '1992', 'F')
         ['KNOTHE55_' tags{k, 1}], vkm.subs.knothe_profile(x, p0, p1, H, etaCur, sc.delta_cur, sc.B_avershin_over_H * H)};
    for c = 1:size(P, 1)
        Q = P{c, 2};
        for j = 1:numel(x)
            fprintf(fid, '%s,%.4f,%.12e,%.12e,%.12e,%.12e,%.12e\n', P{c, 1}, x(j), Q.s(j), Q.tilt(j), ...
                Q.curvature(j), Q.u_h(j), Q.eps_h(j));
        end
    end
end
end

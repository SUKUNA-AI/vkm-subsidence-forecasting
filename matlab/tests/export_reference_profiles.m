function export_reference_profiles(outFile)
%EXPORT_REFERENCE_PROFILES Синтетические профили пакета vkm.subs для сверки с Python-реализацией.
%   export_reference_profiles('tests/world/data/subsidence_matlab_reference.csv') пишет CSV
%   (case, x, s, tilt, curvature, u_h, eps_h); tests/world/test_subsidence_formulas.py пересчитывает те же
%   случаи на Python и сравнивает. Входные числа — синтетические (не СКРУ-1).
x = (-1500:25:1500)';
H = 320; eta = 2.0;
cases = {
    'T4_current_me',   @() vkm.subs.typical_profile(x, -300, 300, H, eta, 'VKM_UKAZANIYA_T4_SRC065', 55, 55, 'current', 'me')
    'M2_1992_F',       @() vkm.subs.typical_profile(x, -300, 300, H, eta, 'VKM1992_T8_26_MODE2', 50, 55, '1992', 'F')
    'M3_1992_avershin', @() vkm.subs.typical_profile(x, -300, 300, H, eta, 'VKM1992_T8_26_MODE3', 50, 55, '1992', 'avershin', 0.16 * H)
    'T4_incomplete',   @() vkm.subs.typical_profile(x, -100, 100, H, eta, 'VKM_UKAZANIYA_T4_SRC065', 55, 55, 'current', 'me')
    'knothe_55',       @() vkm.subs.knothe_profile(x, -300, 300, H, eta, 55, 0.16 * H)
    };
fid = fopen(outFile, 'w');
cleaner = onCleanup(@() fclose(fid));
fprintf(fid, 'case,x,s,tilt,curvature,u_h,eps_h\n');
for k = 1:size(cases, 1)
    P = cases{k, 2}();
    for j = 1:numel(x)
        fprintf(fid, '%s,%.6f,%.12e,%.12e,%.12e,%.12e,%.12e\n', cases{k, 1}, P.x(j), P.s(j), P.tilt(j), ...
            P.curvature(j), P.u_h(j), P.eps_h(j));
    end
end
end

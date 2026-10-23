classdef TestSubsidenceFormulas < matlab.unittest.TestCase
    %TESTSUBSIDENCEFORMULAS Синтетические тесты пакета vkm.subs (предельные случаи, симметрия, непрерывность).
    %   Запуск из корня репозитория:
    %     matlab -batch "maxNumCompThreads(4); addpath('matlab'); r = runtests('matlab/tests'); assertSuccess(r)"
    %   Те же проверки на Python — tests/world/test_subsidence_formulas.py.

    methods (Test)
        function tablesAreConsistent(tc)
            c = vkm.subs.curves();
            names = fieldnames(c);
            for k = 1:numel(names)
                t = c.(names{k});
                tc.verifyEqual(t.z(:), (0:0.1:1)', 'AbsTol', 1e-12);
                if isfield(t, 'S')
                    tc.verifyEqual(t.S(1), 1); tc.verifyEqual(t.S(end), 0);
                    tc.verifyTrue(all(diff(t.S) <= 0));
                end
            end
            tc.verifyEqual(c.VKM_UKAZANIYA_T4_SRC065.S2(:), c.VKM1992_T8_26_MODE1.S2(:));
        end

        function factorsAndContinuity(tc)
            A = @vkm.subs.amplitude;
            tc.verifyEqual(A('backfill', 1, 0.18), 0.18, 'AbsTol', 1e-15);
            tc.verifyEqual(A('k', 0.32), 0.9);
            tc.verifyEqual(A('k', 0.32 - 1e-9), 0.9, 'RelTol', 1e-6);
            tc.verifyEqual(A('k', 0.16), 0.225, 'AbsTol', 1e-12);
            tc.verifyEqual(A('f0', 0.15 + 1e-9), 0, 'AbsTol', 1e-3);
            tc.verifyEqual(A('f0', 1.4 - 1e-9), 1, 'AbsTol', 1e-3);
            tc.verifyEqual(A('q', 45 - 1e-9, 'tilt'), 1.20, 'AbsTol', 1e-3);
            tc.verifyEqual(A('q', 45 - 1e-9, 'curvature'), 1.25, 'AbsTol', 1e-3);
            tc.verifyEqual(A('q', 45 - 1e-9, 'strain'), 1.50, 'AbsTol', 1e-3);
            tc.verifyEqual(A('me', 1e-4), 0.05, 'AbsTol', 1e-15);
            tc.verifyEqual(A('me', 0), 0.14, 'AbsTol', 1e-15);
            tc.verifyEqual(A('chi', 1.4 * 300 - 1e-6, 1e9, 300), 1.4, 'RelTol', 1e-6);
            m = 5.5; a = 16; b = 11;
            tc.verifyEqual(A('eta_1992', m * a, a, b, 0, 0), A('eta_current', m, a / (a + b), 1, 0.9), 'RelTol', 1e-12);
            tc.verifyEqual(A('vzt', 300, 0, 30, 5, 0.4, 0, 0), 2 * 0.002 * 270^2 / (30 * 5 * 0.4), 'RelTol', 1e-12);
            tc.verifyLessThan(A('time', 100, 0.40, 'as_printed'), 0);
            tc.verifyEqual(A('time', 100, 0.40, 'beta15_plus'), A('time', 100, 0.40 + 1e-9), 'RelTol', 2e-3);
        end

        function typicalProfileSymmetryAndBoundary(tc)
            H = 300; x = linspace(-1200, 1200, 2401)';
            P = vkm.subs.typical_profile(x, -400, 400, H, 2.0, 'VKM_UKAZANIYA_T4_SRC065', 55, 55);
            tc.verifyEqual(P.mode, 'full');
            tc.verifyEqual(P.s, flipud(P.s), 'AbsTol', 1e-12);
            tc.verifyEqual(P.tilt, -flipud(P.tilt), 'AbsTol', 1e-12);
            tc.verifyEqual(min(P.s(abs(x) < 400 - H * cotd(55))), 2.0, 'AbsTol', 1e-12);
            tc.verifyTrue(all(P.s(abs(x) >= 400 + H * cotd(55)) == 0));
            nz = P.curvature ~= 0;
            tc.verifyTrue(all(sign(P.eps_h(nz)) == sign(P.curvature(nz))));
        end

        function knotheVolumeAndLimits(tc)
            H = 300; w = 1.5; x = linspace(-3000, 3000, 12001)';
            P = vkm.subs.knothe_profile(x, -200, 200, H, w, 55, 0.16 * H);
            tc.verifyEqual(trapz(x, P.s), w * 400, 'RelTol', 1e-6);
            W = vkm.subs.knothe_profile(x, -1500, 1500, H, w, 55);
            tc.verifyEqual(W.s(6001), w, 'RelTol', 1e-9);
            E = vkm.subs.knothe_profile(0, 0, 1e6, H, w, 55);
            tc.verifyEqual(E.s, w / 2, 'RelTol', 1e-12);
        end
    end
end

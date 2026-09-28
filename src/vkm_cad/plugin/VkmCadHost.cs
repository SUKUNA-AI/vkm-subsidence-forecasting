// VKM CAD bridge v1 - .NET host for headless AutoCAD / Civil 3D jobs (accoreconsole, net8.0, C# 10).
//
// Command VKMHOST reads the request JSON named by the environment variable VKM_CAD_REQUEST, runs its operations in
// order (each in its own transaction; the first failure stops the run), writes the result JSON named by
// VKM_CAD_RESULT and, on failure, the flag file VKM_CAD_FAILED_FLAG (the LISP epilogue then does not save).
// Civil 3D operations (c3d.*) need the Civil 3D product (/product C3D); they touch Autodesk.Civil types only inside
// their own methods, so the host also loads in plain AutoCAD. JobContext.Execute runs user code (command VKMUSER).
// Outputs are derived objects: the bridge labels them (INTERPOLATION / DERIVATION, MODEL_CHOICE, UNKNOWN_CRS).
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using Autodesk.AutoCAD.ApplicationServices;
using Autodesk.AutoCAD.DatabaseServices;
using Autodesk.AutoCAD.EditorInput;
using Autodesk.AutoCAD.Geometry;
using Autodesk.AutoCAD.Runtime;
using CoreApp = Autodesk.AutoCAD.ApplicationServices.Core.Application;

[assembly: CommandClass(typeof(Vkm.Cad.HostCommands))]

namespace Vkm.Cad
{
    public static class J
    {
        public static double D(JsonObject a, string k, double def)
        {
            var n = a[k];
            return n == null ? def : n.GetValue<double>();
        }

        public static double? DN(JsonObject a, string k)
        {
            var n = a[k];
            return n == null ? (double?)null : n.GetValue<double>();
        }

        public static string S(JsonObject a, string k, string def)
        {
            var n = a[k];
            return n == null ? def : n.GetValue<string>();
        }

        public static bool B(JsonObject a, string k, bool def)
        {
            var n = a[k];
            return n == null ? def : n.GetValue<bool>();
        }

        public static JsonArray A(JsonObject a, string k)
        {
            var n = a[k];
            return n == null ? new JsonArray() : n.AsArray();
        }

        public static double[] P(JsonNode n)
        {
            var arr = n.AsArray();
            var o = new double[3];
            for (int i = 0; i < Math.Min(3, arr.Count); i++) o[i] = arr[i].GetValue<double>();
            return o;
        }

        public static JsonArray Pt(Point3d p, bool z = true)
        {
            return z ? new JsonArray(R(p.X), R(p.Y), R(p.Z)) : new JsonArray(R(p.X), R(p.Y));
        }

        public static double R(double v)
        {
            return Math.Round(v, 6);
        }

        public static JsonNode ToNode(object value)
        {
            if (value == null) return null;
            if (value is JsonNode node) return node;
            if (value is string s) return JsonValue.Create(s);
            if (value is bool b) return JsonValue.Create(b);
            if (value is int i) return JsonValue.Create(i);
            if (value is long l) return JsonValue.Create(l);
            if (value is uint ui) return JsonValue.Create(ui);
            if (value is double d) return double.IsFinite(d) ? JsonValue.Create(d) : null;
            if (value is float f) return JsonValue.Create((double)f);
            if (value is Point3d p3) return Pt(p3);
            if (value is Point2d p2) return new JsonArray(R(p2.X), R(p2.Y));
            if (value is Vector3d v3) return new JsonArray(R(v3.X), R(v3.Y), R(v3.Z));
            if (value is ObjectId id) return JsonValue.Create(id.IsNull ? null : id.Handle.ToString());
            if (value is IDictionary dict)
            {
                var o = new JsonObject();
                foreach (DictionaryEntry e in dict) o[Convert.ToString(e.Key, CultureInfo.InvariantCulture)] = ToNode(e.Value);
                return o;
            }
            if (value is IEnumerable seq)
            {
                var arr = new JsonArray();
                foreach (var item in seq) arr.Add(ToNode(item));
                return arr;
            }
            return JsonValue.Create(Convert.ToString(value, CultureInfo.InvariantCulture));
        }

        public static void WriteJson(string path, JsonNode node)
        {
            var text = node.ToJsonString(new JsonSerializerOptions { WriteIndented = true });
            File.WriteAllText(path, text, new UTF8Encoding(false));
        }

        public static void Flag(string reason)
        {
            var flag = Environment.GetEnvironmentVariable("VKM_CAD_FAILED_FLAG");
            if (!string.IsNullOrEmpty(flag)) File.AppendAllText(flag, reason + "\n", new UTF8Encoding(false));
        }
    }

    public static class Cad
    {
        public static ObjectId EnsureLayer(Database db, Transaction tr, string name, short color)
        {
            var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
            if (lt.Has(name)) return lt[name];
            lt.UpgradeOpen();
            var rec = new LayerTableRecord { Name = name };
            rec.Color = Autodesk.AutoCAD.Colors.Color.FromColorIndex(Autodesk.AutoCAD.Colors.ColorMethod.ByAci, color);
            var id = lt.Add(rec);
            tr.AddNewlyCreatedDBObject(rec, true);
            return id;
        }

        public static ObjectId EnsureTextStyle(Database db, Transaction tr, string name, string font)
        {
            var st = (TextStyleTable)tr.GetObject(db.TextStyleTableId, OpenMode.ForRead);
            if (st.Has(name)) return st[name];
            st.UpgradeOpen();
            var rec = new TextStyleTableRecord { Name = name };
            rec.Font = new Autodesk.AutoCAD.GraphicsInterface.FontDescriptor(font, false, false, 0, 0);
            var id = st.Add(rec);
            tr.AddNewlyCreatedDBObject(rec, true);
            return id;
        }

        public static BlockTableRecord ModelSpace(Database db, Transaction tr)
        {
            return (BlockTableRecord)tr.GetObject(SymbolUtilityServices.GetBlockModelSpaceId(db), OpenMode.ForWrite);
        }

        public static ObjectId Add(BlockTableRecord space, Transaction tr, Entity e, string layer)
        {
            if (layer != null) e.Layer = layer;
            var id = space.AppendEntity(e);
            tr.AddNewlyCreatedDBObject(e, true);
            return id;
        }

        public static Polyline Pline2d(IList<double[]> pts, bool closed, double elevation)
        {
            var pl = new Polyline();
            for (int i = 0; i < pts.Count; i++) pl.AddVertexAt(i, new Point2d(pts[i][0], pts[i][1]), 0, 0, 0);
            pl.Closed = closed;
            pl.Elevation = elevation;
            return pl;
        }

        public static JsonObject Extents(Database db)
        {
            db.UpdateExt(true);
            return new JsonObject { ["min"] = J.Pt(db.Extmin), ["max"] = J.Pt(db.Extmax) };
        }
    }

    public sealed class JobContext
    {
        public Document Doc;
        public Database Db;
        public Editor Ed;
        public Transaction Tr;
        public string RunDir;
        public List<string> Messages = new List<string>();

        public void Log(string message)
        {
            Messages.Add(message);
        }

        public static void Execute(Func<JobContext, object> fn)
        {
            var resultPath = Environment.GetEnvironmentVariable("VKM_CAD_RESULT");
            var result = new JsonObject { ["schema"] = "vkm-cad.user_result/1" };
            var ctx = new JobContext();
            var sw = Stopwatch.StartNew();
            try
            {
                ctx.Doc = CoreApp.DocumentManager.MdiActiveDocument;
                ctx.Db = ctx.Doc.Database;
                ctx.Ed = ctx.Doc.Editor;
                ctx.RunDir = Environment.GetEnvironmentVariable("VKM_CAD_RUN_DIR");
                using (var tr = ctx.Db.TransactionManager.StartTransaction())
                {
                    ctx.Tr = tr;
                    var value = fn(ctx);
                    tr.Commit();
                    result["result"] = J.ToNode(value);
                }
                result["ok"] = true;
            }
            catch (System.Exception ex)
            {
                result["ok"] = false;
                result["error"] = ex.GetType().Name + ": " + ex.Message;
                J.Flag("CSHARP " + ex.GetType().Name + ": " + ex.Message);
            }
            result["log"] = J.ToNode(ctx.Messages);
            result["ms"] = sw.ElapsedMilliseconds;
            if (!string.IsNullOrEmpty(resultPath)) J.WriteJson(resultPath, result);
        }
    }

    public static class HostCommands
    {
        [CommandMethod("VKMHOST", CommandFlags.Modal)]
        public static void Run()
        {
            var reqPath = Environment.GetEnvironmentVariable("VKM_CAD_REQUEST");
            var resPath = Environment.GetEnvironmentVariable("VKM_CAD_RESULT");
            var result = new JsonObject { ["schema"] = "vkm-cad.host_result/1" };
            var opsOut = new JsonArray();
            result["ops"] = opsOut;
            bool ok = true;
            try
            {
                var doc = CoreApp.DocumentManager.MdiActiveDocument;
                result["acadver"] = Convert.ToString(CoreApp.GetSystemVariable("ACADVER"), CultureInfo.InvariantCulture);
                var request = JsonNode.Parse(File.ReadAllText(reqPath, Encoding.UTF8)).AsObject();
                foreach (var node in request["ops"].AsArray())
                {
                    var op = node.AsObject();
                    var name = op["op"].GetValue<string>();
                    var args = op["args"] as JsonObject ?? new JsonObject();
                    var entry = new JsonObject { ["op"] = name };
                    var sw = Stopwatch.StartNew();
                    try
                    {
                        entry["result"] = Dispatch(name, args, doc);
                        entry["ok"] = true;
                    }
                    catch (System.Exception ex)
                    {
                        ok = false;
                        entry["ok"] = false;
                        entry["error"] = ex.GetType().Name + ": " + ex.Message;
                    }
                    entry["ms"] = sw.ElapsedMilliseconds;
                    opsOut.Add(entry);
                    if (!ok) break;
                }
            }
            catch (System.Exception ex)
            {
                ok = false;
                result["error"] = ex.GetType().Name + ": " + ex.Message;
            }
            result["ok"] = ok;
            if (!string.IsNullOrEmpty(resPath)) J.WriteJson(resPath, result);
            if (!ok) J.Flag("HOST " + (result["error"] != null ? result["error"].ToString() : "operation failed"));
        }

        static JsonNode Dispatch(string name, JsonObject a, Document doc)
        {
            switch (name)
            {
                case "acad.info": return CoreOps.Info(doc, a);
                case "acad.layout_sheet": return CoreOps.LayoutSheet(doc, a);
                case "c3d.points": return CivilOps.Points(doc, a);
                case "c3d.tin": return CivilOps.Tin(doc, a);
                case "c3d.contours": return CivilOps.Contours(doc, a);
                case "c3d.volume": return CivilOps.Volume(doc, a);
                case "c3d.alignment_profile": return CivilOps.AlignmentProfile(doc, a);
                case "c3d.list": return CivilOps.List(doc, a);
                default: throw new ArgumentException("unknown operation " + name);
            }
        }
    }

    public static class CoreOps
    {
        public static JsonNode Info(Document doc, JsonObject a)
        {
            var db = doc.Database;
            var byType = new SortedDictionary<string, int>();
            var byLayer = new SortedDictionary<string, int>();
            var layouts = new JsonArray();
            var layers = new JsonArray();
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var ms = (BlockTableRecord)tr.GetObject(SymbolUtilityServices.GetBlockModelSpaceId(db), OpenMode.ForRead);
                foreach (ObjectId id in ms)
                {
                    var e = (Entity)tr.GetObject(id, OpenMode.ForRead);
                    var t = e.GetRXClass().DxfName;
                    if (string.IsNullOrEmpty(t)) t = e.GetRXClass().Name;
                    byType[t] = byType.TryGetValue(t, out var c1) ? c1 + 1 : 1;
                    byLayer[e.Layer] = byLayer.TryGetValue(e.Layer, out var c2) ? c2 + 1 : 1;
                }
                var dict = (DBDictionary)tr.GetObject(db.LayoutDictionaryId, OpenMode.ForRead);
                foreach (DBDictionaryEntry entry in dict) layouts.Add(entry.Key);
                var lt = (LayerTable)tr.GetObject(db.LayerTableId, OpenMode.ForRead);
                foreach (ObjectId id in lt) layers.Add(((LayerTableRecord)tr.GetObject(id, OpenMode.ForRead)).Name);
                tr.Commit();
            }
            return new JsonObject
            {
                ["extents"] = Cad.Extents(db), ["insunits"] = (int)db.Insunits,
                ["by_type"] = J.ToNode(byType), ["by_layer"] = J.ToNode(byLayer), ["layouts"] = layouts,
                ["layers"] = layers
            };
        }

        // A paper-space sheet: page setup (DWG To PDF.pc3, canonical media, 1:1), a frame, one viewport at 1:N and
        // a simplified title block after GOST 2.104 form 1 (185 x 55 mm).
        public static JsonNode LayoutSheet(Document doc, JsonObject a)
        {
            var stage = new string[] { "start" };
            try { return LayoutSheetSteps(doc, a, stage); }
            catch (System.Exception ex) { throw new InvalidOperationException("layout sheet failed at '" + stage[0] + "': " + ex.Message, ex); }
        }

        static JsonNode LayoutSheetSteps(Document doc, JsonObject a, string[] stage)
        {
            var db = doc.Database;
            string name = J.S(a, "name", "VKM_SHEET");
            string device = J.S(a, "device", "DWG To PDF.pc3");
            string media = J.S(a, "media", "ISO_full_bleed_A3_(420.00_x_297.00_MM)");
            string styleTable = J.S(a, "style_table", "monochrome.ctb");
            int rotation = (int)J.D(a, "rotation", 0);
            double unitMm = J.D(a, "paper_mm_per_model_unit", 1000.0);
            double? denominator = J.DN(a, "scale_denominator");
            var lm = LayoutManager.Current;
            bool overwrite = J.B(a, "overwrite", false);
            stage[0] = "layout";
            ObjectId existing = lm.GetLayoutId(name);
            if (!existing.IsNull)
            {
                if (!overwrite) throw new InvalidOperationException("layout " + name + " exists (overwrite=false)");
                lm.DeleteLayout(name);
            }
            ObjectId layoutId = lm.CreateLayout(name);
            stage[0] = "activate layout";
            lm.CurrentLayout = name;
            var outp = new JsonObject { ["layout"] = name };
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var lay = (Layout)tr.GetObject(layoutId, OpenMode.ForWrite);
                var psv = PlotSettingsValidator.Current;
                stage[0] = "plot device " + device + " / media " + media;
                psv.SetPlotConfigurationName(lay, device, media);
                psv.RefreshLists(lay);
                stage[0] = "paper units";
                psv.SetPlotPaperUnits(lay, PlotPaperUnit.Millimeters);
                stage[0] = "rotation";
                psv.SetPlotRotation(lay, rotation == 90 ? PlotRotation.Degrees090 : PlotRotation.Degrees000);
                stage[0] = "plot type";
                psv.SetPlotType(lay, Autodesk.AutoCAD.DatabaseServices.PlotType.Layout);
                stage[0] = "scale 1:1";
                psv.SetUseStandardScale(lay, true);
                psv.SetStdScaleType(lay, StdScaleType.StdScale1To1);
                stage[0] = "origin";
                psv.SetPlotOrigin(lay, new Point2d(0, 0));
                stage[0] = "style sheet";
                try { psv.SetCurrentStyleSheet(lay, styleTable); } catch (System.Exception) { outp["style_table_warning"] = styleTable + " not set"; }
                lay.PrintLineweights = true;
                stage[0] = "geometry";
                double w = lay.PlotPaperSize.X, h = lay.PlotPaperSize.Y;
                if (rotation == 90 || (lay.PlotRotation == PlotRotation.Degrees090)) { var t = w; w = h; h = t; }
                if (J.B(a, "landscape", true) && h > w) { var t = w; w = h; h = t; }
                outp["media"] = lay.CanonicalMediaName;
                outp["paper_mm"] = new JsonArray(J.R(w), J.R(h));
                var ps = (BlockTableRecord)tr.GetObject(lay.BlockTableRecordId, OpenMode.ForWrite);
                foreach (ObjectId id in ps)
                {
                    var vp0 = tr.GetObject(id, OpenMode.ForRead) as Viewport;
                    if (vp0 != null && vp0.Number != 1) { vp0.UpgradeOpen(); vp0.Erase(); }
                }
                var m = J.A(a, "margins_mm");
                double ml = m.Count == 4 ? m[0].GetValue<double>() : 20, mr = m.Count == 4 ? m[1].GetValue<double>() : 5;
                double mt = m.Count == 4 ? m[2].GetValue<double>() : 5, mb = m.Count == 4 ? m[3].GetValue<double>() : 5;
                string frameLayer = "VKM_SHEET_FRAME";
                Cad.EnsureLayer(db, tr, frameLayer, 7);
                var frame = Cad.Pline2d(new List<double[]> { new[] { ml, mb }, new[] { w - mr, mb }, new[] { w - mr, h - mt }, new[] { ml, h - mt } }, true, 0);
                frame.LineWeight = LineWeight.LineWeight070;
                Cad.Add(ps, tr, frame, frameLayer);
                bool stamp = a["title_block"] != null;
                double stampH = stamp ? 55 : 0;
                // viewport rectangle on paper
                var rect = J.A(a, "viewport_rect_mm");
                double x0 = rect.Count == 4 ? rect[0].GetValue<double>() : ml + 5;
                double y0 = rect.Count == 4 ? rect[1].GetValue<double>() : mb + stampH + 5;
                double x1 = rect.Count == 4 ? rect[2].GetValue<double>() : w - mr - 5;
                double y1 = rect.Count == 4 ? rect[3].GetValue<double>() : h - mt - 5;
                double vw = x1 - x0, vh = y1 - y0;
                if (vw <= 10 || vh <= 10) throw new ArgumentException("viewport rectangle is too small");
                // model window
                db.UpdateExt(true);
                double cx, cy, mw, mh;
                var win = J.A(a, "model_window");
                if (win.Count == 2)
                {
                    var p0 = J.P(win[0]); var p1 = J.P(win[1]);
                    cx = (p0[0] + p1[0]) / 2; cy = (p0[1] + p1[1]) / 2; mw = Math.Abs(p1[0] - p0[0]); mh = Math.Abs(p1[1] - p0[1]);
                }
                else
                {
                    cx = (db.Extmin.X + db.Extmax.X) / 2; cy = (db.Extmin.Y + db.Extmax.Y) / 2;
                    mw = Math.Max(1e-9, db.Extmax.X - db.Extmin.X); mh = Math.Max(1e-9, db.Extmax.Y - db.Extmin.Y);
                }
                double scale;            // paper mm per model unit
                if (denominator.HasValue && denominator.Value > 0) scale = unitMm / denominator.Value;
                else scale = Math.Min(vw / mw, vh / mh) * 0.95;
                stage[0] = "viewport";
                var vp = new Viewport();
                vp.CenterPoint = new Point3d((x0 + x1) / 2, (y0 + y1) / 2, 0);
                vp.Width = vw;
                vp.Height = vh;
                Cad.Add(ps, tr, vp, frameLayer);
                vp.ViewDirection = Vector3d.ZAxis;
                vp.ViewTarget = new Point3d(cx, cy, 0);
                vp.ViewCenter = new Point2d(0, 0);
                vp.CustomScale = scale;
                vp.On = true;
                vp.Locked = true;
                outp["viewport_rect_mm"] = new JsonArray(J.R(x0), J.R(y0), J.R(x1), J.R(y1));
                outp["model_center"] = new JsonArray(J.R(cx), J.R(cy));
                outp["paper_mm_per_model_unit"] = J.R(scale);
                outp["scale_denominator"] = J.R(unitMm / scale);
                outp["scale_is_fit"] = !(denominator.HasValue && denominator.Value > 0);
                stage[0] = "title block";
                if (stamp) TitleBlock(db, tr, ps, (JsonObject)a["title_block"], w - mr, mb, outp);
                stage[0] = "commit";
                tr.Commit();
            }
            stage[0] = "back to model";
            lm.CurrentLayout = "Model";
            return outp;
        }

        // simplified main inscription after GOST 2.104 form 1: 185 x 55 mm, anchored at its lower-right corner
        static void TitleBlock(Database db, Transaction tr, BlockTableRecord ps, JsonObject f, double right, double bottom, JsonObject outp)
        {
            string layer = "VKM_SHEET_STAMP";
            Cad.EnsureLayer(db, tr, layer, 7);
            ObjectId style = Cad.EnsureTextStyle(db, tr, "VKM_GOST", "Arial");
            double X(double x) { return right - 185 + x; }
            double Y(double y) { return bottom + y; }
            void L(double xa, double ya, double xb, double yb, bool thick)
            {
                var ln = new Line(new Point3d(X(xa), Y(ya), 0), new Point3d(X(xb), Y(yb), 0));
                ln.LineWeight = thick ? LineWeight.LineWeight050 : LineWeight.LineWeight018;
                Cad.Add(ps, tr, ln, layer);
            }
            void T(double x, double y, string text, double height)
            {
                if (string.IsNullOrEmpty(text)) return;
                var t = new DBText { TextString = text, Height = height, TextStyleId = style };
                t.Position = new Point3d(X(x), Y(y), 0);
                Cad.Add(ps, tr, t, layer);
            }
            string F(string key) { var n = f[key]; return n == null ? "" : n.ToString(); }
            // outline and main divisions
            L(0, 0, 185, 0, true); L(185, 0, 185, 55, true); L(185, 55, 0, 55, true); L(0, 55, 0, 0, true);
            L(65, 0, 65, 55, true); L(65, 40, 185, 40, true); L(65, 15, 135, 15, true); L(135, 0, 135, 40, true);
            L(135, 35, 185, 35, false); L(135, 20, 185, 20, true); L(135, 15, 185, 15, true);
            L(150, 20, 150, 40, false); L(167, 20, 167, 40, false); L(155, 15, 155, 20, false);
            // left block: 11 rows x 5 mm, columns 7 / 10 / 23 / 15 / 10
            for (int i = 1; i < 11; i++) L(0, 5 * i, 65, 5 * i, i == 8 || i == 7);
            foreach (var x in new double[] { 7, 17, 40, 55 }) L(x, 30, x, 55, true);
            L(17, 0, 17, 30, true); L(40, 0, 40, 30, true); L(55, 0, 55, 30, true);
            T(0.8, 36.3, "Изм.", 2.2); T(7.8, 36.3, "Лист", 2.2); T(18.5, 36.3, "№ докум.", 2.2); T(41, 36.3, "Подп.", 2.2); T(56, 36.3, "Дата", 2.2);
            T(0.8, 26.3, "Разраб.", 2.2); T(0.8, 21.3, "Пров.", 2.2); T(0.8, 16.3, "Т.контр.", 2.2); T(0.8, 6.3, "Н.контр.", 2.2); T(0.8, 1.3, "Утв.", 2.2);
            T(18.5, 26.3, F("developer"), 2.5); T(18.5, 21.3, F("checker"), 2.5); T(18.5, 1.3, F("approver"), 2.5);
            T(56, 26.3, F("date"), 2.2);
            T(137, 36.3, "Лит.", 2.2); T(151, 36.3, "Масса", 2.2); T(168, 36.3, "Масштаб", 2.2);
            T(169, 25, F("scale"), 3.5);
            T(136, 16.3, "Лист " + F("sheet"), 2.2); T(156, 16.3, "Листов " + F("sheets"), 2.2);
            T(68, 45, F("designation"), 5);
            T(67, 30, F("title"), 3.5); T(67, 22, F("subtitle"), 2.5);
            T(137, 6, F("organization"), 2.5);
            T(67, 6, F("material"), 2.5);
            outp["title_block"] = "GOST 2.104 form 1 (simplified), 185x55 mm";
        }
    }

    public static class CivilOps
    {
        static Autodesk.Civil.ApplicationServices.CivilDocument Civil(Database db)
        {
            return Autodesk.Civil.ApplicationServices.CivilDocument.GetCivilDocument(db);
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        public static JsonNode Points(Document doc, JsonObject a)
        {
            var db = doc.Database;
            var civ = Civil(db);
            var rows = J.A(a, "rows");
            string group = J.S(a, "point_group", null);
            string policy = J.S(a, "name_policy", "as_is");
            var numbers = new List<uint>();
            int rowIndex = 0;
            int skipped = 0;
            double minx = double.MaxValue, miny = double.MaxValue, minz = double.MaxValue;
            double maxx = double.MinValue, maxy = double.MinValue, maxz = double.MinValue;
            using (var tr = db.TransactionManager.StartTransaction())
            {
                foreach (var node in rows)
                {
                    rowIndex++;
                    var r = node.AsObject();
                    if (r["z"] == null) { skipped++; continue; }
                    double x = r["x"].GetValue<double>(), y = r["y"].GetValue<double>(), z = r["z"].GetValue<double>();
                    string desc = J.S(r, "desc", "") ?? "";
                    var id = civ.CogoPoints.Add(new Point3d(x, y, z), desc, true);
                    var cp = (Autodesk.Civil.DatabaseServices.CogoPoint)tr.GetObject(id, OpenMode.ForWrite);
                    string pname = J.S(r, "name", null);
                    if (!string.IsNullOrEmpty(pname) && policy != "none")
                    {
                        string target = policy == "group_prefix" && !string.IsNullOrEmpty(group) ? group + "_" + pname : pname;
                        try { cp.PointName = target; }
                        catch (ArgumentException)
                        {
                            throw new ArgumentException("point name '" + target + "' (row " + rowIndex + ") is invalid or already used in the drawing; " +
                                                        "COGO point names are unique per drawing: pass name_policy='group_prefix' or 'none'");
                        }
                    }
                    numbers.Add(cp.PointNumber);
                    minx = Math.Min(minx, x); miny = Math.Min(miny, y); minz = Math.Min(minz, z);
                    maxx = Math.Max(maxx, x); maxy = Math.Max(maxy, y); maxz = Math.Max(maxz, z);
                }
                string ranges = Ranges(numbers);
                if (!string.IsNullOrEmpty(group) && numbers.Count > 0)
                {
                    if (civ.PointGroups.Contains(group)) throw new InvalidOperationException("point group " + group + " exists");
                    var gid = civ.PointGroups.Add(group);
                    var pg = (Autodesk.Civil.DatabaseServices.PointGroup)tr.GetObject(gid, OpenMode.ForWrite);
                    var q = new Autodesk.Civil.DatabaseServices.StandardPointGroupQuery();
                    q.IncludeNumbers = ranges;
                    pg.SetQuery(q);
                    pg.Update();
                }
                tr.Commit();
                var outp = new JsonObject
                {
                    ["added"] = numbers.Count, ["skipped_no_z"] = skipped, ["point_numbers"] = ranges,
                    ["point_group"] = group, ["total_points"] = (int)civ.CogoPoints.Count, ["name_policy"] = policy
                };
                if (numbers.Count > 0)
                    outp["extents"] = new JsonObject { ["min"] = new JsonArray(J.R(minx), J.R(miny), J.R(minz)), ["max"] = new JsonArray(J.R(maxx), J.R(maxy), J.R(maxz)) };
                return outp;
            }
        }

        static string Ranges(List<uint> numbers)
        {
            if (numbers.Count == 0) return "";
            var sorted = numbers.Distinct().OrderBy(n => n).ToList();
            var parts = new List<string>();
            uint start = sorted[0], prev = sorted[0];
            for (int i = 1; i <= sorted.Count; i++)
            {
                if (i < sorted.Count && sorted[i] == prev + 1) { prev = sorted[i]; continue; }
                parts.Add(start == prev ? start.ToString(CultureInfo.InvariantCulture) : start + "-" + prev);
                if (i < sorted.Count) { start = sorted[i]; prev = sorted[i]; }
            }
            return string.Join(",", parts);
        }

        static ObjectId SurfaceId(Autodesk.Civil.ApplicationServices.CivilDocument civ, Transaction tr, string name)
        {
            foreach (ObjectId id in civ.GetSurfaceIds())
            {
                var s = (Autodesk.Civil.DatabaseServices.Surface)tr.GetObject(id, OpenMode.ForRead);
                if (s.Name == name) return id;
            }
            throw new ArgumentException("surface " + name + " not found");
        }

        static JsonObject SurfaceStats(Autodesk.Civil.DatabaseServices.Surface s)
        {
            var gp = s.GetGeneralProperties();
            var o = new JsonObject
            {
                ["name"] = s.Name, ["type"] = s.GetType().Name, ["points"] = gp.NumberOfPoints,
                ["z_min"] = J.R(gp.MinimumElevation), ["z_max"] = J.R(gp.MaximumElevation), ["z_mean"] = J.R(gp.MeanElevation),
                ["x_min"] = J.R(gp.MinimumCoordinateX), ["y_min"] = J.R(gp.MinimumCoordinateY),
                ["x_max"] = J.R(gp.MaximumCoordinateX), ["y_max"] = J.R(gp.MaximumCoordinateY)
            };
            var tin = s as Autodesk.Civil.DatabaseServices.TinSurface;
            if (tin != null)
            {
                var tp = tin.GetTinProperties();
                o["triangles"] = tp.NumberOfTriangles;
                o["max_triangle_edge"] = J.R(tp.MaximumTriangleLength);
                var terrain = tin.GetTerrainProperties();
                o["area_2d"] = J.R(terrain.SurfaceArea2D);
                o["area_3d"] = J.R(terrain.SurfaceArea3D);
            }
            return o;
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        public static JsonNode Tin(Document doc, JsonObject a)
        {
            var db = doc.Database;
            var civ = Civil(db);
            string name = J.S(a, "name", "VKM_TIN");
            string layer = J.S(a, "layer", "VKM_SURFACE");
            using (var tr = db.TransactionManager.StartTransaction())
            {
                foreach (ObjectId sid0 in civ.GetSurfaceIds())
                    if (((Autodesk.Civil.DatabaseServices.Surface)tr.GetObject(sid0, OpenMode.ForRead)).Name == name)
                        throw new InvalidOperationException("surface " + name + " exists");
                Cad.EnsureLayer(db, tr, layer, 3);
                var sid = Autodesk.Civil.DatabaseServices.TinSurface.Create(db, name);
                var s = (Autodesk.Civil.DatabaseServices.TinSurface)tr.GetObject(sid, OpenMode.ForWrite);
                s.Layer = layer;
                double? maxEdge = J.DN(a, "max_triangle_length");
                if (maxEdge.HasValue && maxEdge.Value > 0)
                {
                    s.BuildOptions.UseMaximumTriangleLength = true;
                    s.BuildOptions.MaximumTriangleLength = maxEdge.Value;
                }
                string group = J.S(a, "point_group", null);
                if (!string.IsNullOrEmpty(group))
                {
                    if (!civ.PointGroups.Contains(group)) throw new ArgumentException("point group " + group + " not found");
                    s.PointGroupsDefinition.AddPointGroup(civ.PointGroups[group]);
                }
                var pts = J.A(a, "points");
                if (pts.Count > 0)
                {
                    var col = new Point3dCollection();
                    foreach (var n in pts) { var p = J.P(n); col.Add(new Point3d(p[0], p[1], p[2])); }
                    s.AddVertices(col);
                }
                var ms = Cad.ModelSpace(db, tr);
                var breaks = J.A(a, "breaklines");
                if (breaks.Count > 0)
                {
                    Cad.EnsureLayer(db, tr, "VKM_BREAKLINES", 1);
                    var ids = new ObjectIdCollection();
                    foreach (var line in breaks)
                    {
                        var col = new Point3dCollection();
                        foreach (var n in line.AsArray()) { var p = J.P(n); col.Add(new Point3d(p[0], p[1], p[2])); }
                        var pl = new Polyline3d(Poly3dType.SimplePoly, col, false);
                        ids.Add(Cad.Add(ms, tr, pl, "VKM_BREAKLINES"));
                    }
                    s.BreaklinesDefinition.AddStandardBreaklines(ids, 1.0, 0.0, 0.0, 0.0);
                }
                var boundary = J.A(a, "boundary");
                if (boundary.Count >= 3)
                {
                    Cad.EnsureLayer(db, tr, "VKM_BOUNDARY", 6);
                    var list = boundary.Select(n => J.P(n)).ToList();
                    var pl = Cad.Pline2d(list, true, 0);
                    var ids = new ObjectIdCollection { Cad.Add(ms, tr, pl, "VKM_BOUNDARY") };
                    s.BoundariesDefinition.AddBoundaries(ids, 1.0, Autodesk.Civil.SurfaceBoundaryType.Outer, true);
                }
                s.Rebuild();
                var stats = SurfaceStats(s);
                int cap = (int)J.D(a, "max_triangles_export", 200000);
                if (J.B(a, "export_triangles", true))
                {
                    var tris = new JsonArray();
                    int n = 0;
                    foreach (Autodesk.Civil.DatabaseServices.TinSurfaceTriangle t in s.GetTriangles(false))
                    {
                        if (++n > cap) break;
                        tris.Add(new JsonArray(J.Pt(t.Vertex1.Location), J.Pt(t.Vertex2.Location), J.Pt(t.Vertex3.Location)));
                    }
                    stats["triangles_export"] = tris;
                    stats["triangles_export_truncated"] = n > cap;
                }
                tr.Commit();
                return stats;
            }
        }

        static ObjectIdCollection ContoursAtMultiples(Autodesk.Civil.DatabaseServices.TinSurface s, double interval)
        {
            var gp = s.GetGeneralProperties();
            var all = new ObjectIdCollection();
            long first = (long)Math.Ceiling(gp.MinimumElevation / interval - 1e-9);
            long last = (long)Math.Floor(gp.MaximumElevation / interval + 1e-9);
            if (last - first > 100000) throw new ArgumentException("more than 100000 contour levels");
            for (long k = first; k <= last; k++)
            {
                double level = Math.Round(k * interval, 10);
                if (level <= gp.MinimumElevation || level >= gp.MaximumElevation) continue;
                foreach (ObjectId id in s.ExtractContoursAt(level)) all.Add(id);
            }
            return all;
        }

        static JsonArray PolylineNodes(Transaction tr, ObjectIdCollection ids, string minorLayer, string majorLayer, double major, out int nMajor)
        {
            var list = new JsonArray();
            nMajor = 0;
            foreach (ObjectId id in ids)
            {
                var ent = (Entity)tr.GetObject(id, OpenMode.ForWrite);
                var pts = new JsonArray();
                double elev = double.NaN;
                bool closed = false;
                if (ent is Polyline pl)
                {
                    elev = pl.Elevation; closed = pl.Closed;
                    for (int i = 0; i < pl.NumberOfVertices; i++) { var p = pl.GetPoint2dAt(i); pts.Add(new JsonArray(J.R(p.X), J.R(p.Y))); }
                }
                else if (ent is Polyline2d p2)
                {
                    elev = p2.Elevation; closed = p2.Closed;
                    foreach (ObjectId vid in p2) { var v = (Vertex2d)tr.GetObject(vid, OpenMode.ForRead); pts.Add(new JsonArray(J.R(v.Position.X), J.R(v.Position.Y))); }
                }
                else if (ent is Polyline3d p3)
                {
                    closed = p3.Closed;
                    foreach (ObjectId vid in p3) { var v = (PolylineVertex3d)tr.GetObject(vid, OpenMode.ForRead); pts.Add(new JsonArray(J.R(v.Position.X), J.R(v.Position.Y))); elev = v.Position.Z; }
                }
                bool isMajor = major > 0 && !double.IsNaN(elev) && Math.Abs(elev / major - Math.Round(elev / major)) < 1e-6;
                if (isMajor) nMajor++;
                ent.Layer = isMajor ? majorLayer : minorLayer;
                list.Add(new JsonObject { ["elevation"] = double.IsNaN(elev) ? null : J.R(elev), ["closed"] = closed, ["major"] = isMajor, ["points"] = pts });
            }
            return list;
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        public static JsonNode Contours(Document doc, JsonObject a)
        {
            var db = doc.Database;
            var civ = Civil(db);
            string name = J.S(a, "surface", null);
            double interval = J.D(a, "interval", 1.0);
            double major = J.D(a, "major_interval", 0.0);
            string minorLayer = J.S(a, "layer_minor", "VKM_CONTOUR_MINOR");
            string majorLayer = J.S(a, "layer_major", "VKM_CONTOUR_MAJOR");
            if (interval <= 0) throw new ArgumentException("interval must be positive");
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var sid = SurfaceId(civ, tr, name);
                var s = tr.GetObject(sid, OpenMode.ForWrite) as Autodesk.Civil.DatabaseServices.TinSurface;
                if (s == null) throw new ArgumentException("contours are extracted from TIN surfaces only");
                Cad.EnsureLayer(db, tr, minorLayer, 8);
                Cad.EnsureLayer(db, tr, majorLayer, 5);
                // levels at multiples of the interval (ExtractContours(interval) starts at the surface minimum)
                ObjectIdCollection ids = ContoursAtMultiples(s, interval);
                var list = PolylineNodes(tr, ids, minorLayer, majorLayer, major, out int nMajor);
                tr.Commit();
                var levels = list.Select(n => n["elevation"]).Where(e => e != null).Select(e => e.GetValue<double>()).Distinct().OrderBy(e => e).ToList();
                return new JsonObject
                {
                    ["surface"] = name, ["interval"] = interval, ["major_interval"] = major, ["count"] = list.Count,
                    ["major_count"] = nMajor, ["levels"] = J.ToNode(levels), ["polylines"] = list
                };
            }
        }

        // Difference of two TIN surfaces: a Civil 3D TIN volume surface (cut/fill volumes) and a TIN of dz values
        // sampled at the union of both vertex sets (dz = compare - base), whose contours are the dz isolines.
        [MethodImpl(MethodImplOptions.NoInlining)]
        public static JsonNode Volume(Document doc, JsonObject a)
        {
            var db = doc.Database;
            var civ = Civil(db);
            string name = J.S(a, "name", "VKM_DIFF");
            string dzName = J.S(a, "dz_surface", name + "_DZ");
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var baseId = SurfaceId(civ, tr, J.S(a, "base", null));
                var compId = SurfaceId(civ, tr, J.S(a, "compare", null));
                var b = tr.GetObject(baseId, OpenMode.ForRead) as Autodesk.Civil.DatabaseServices.TinSurface;
                var c = tr.GetObject(compId, OpenMode.ForRead) as Autodesk.Civil.DatabaseServices.TinSurface;
                if (b == null || c == null) throw new ArgumentException("base and compare must be TIN surfaces");
                var vid = Autodesk.Civil.DatabaseServices.TinVolumeSurface.Create(name, baseId, compId);
                var v = (Autodesk.Civil.DatabaseServices.TinVolumeSurface)tr.GetObject(vid, OpenMode.ForWrite);
                Cad.EnsureLayer(db, tr, J.S(a, "layer", "VKM_DIFFERENCE"), 1);
                v.Layer = J.S(a, "layer", "VKM_DIFFERENCE");
                var vp = v.GetVolumeProperties();
                var outp = new JsonObject
                {
                    ["volume_surface"] = SurfaceStats(v), ["cut_volume"] = J.R(vp.UnadjustedCutVolume),
                    ["fill_volume"] = J.R(vp.UnadjustedFillVolume), ["net_volume"] = J.R(vp.UnadjustedNetVolume),
                    ["sign_convention"] = "dz = compare - base; cut = compare below base (subsidence)"
                };
                // dz TIN from the union of vertices inside both surfaces
                var seen = new HashSet<string>();
                var col = new Point3dCollection();
                int outside = 0;
                foreach (var surf in new[] { b, c })
                {
                    foreach (Autodesk.Civil.DatabaseServices.TinSurfaceVertex vx in surf.Vertices)
                    {
                        var p = vx.Location;
                        var key = Math.Round(p.X, 6).ToString(CultureInfo.InvariantCulture) + "|" + Math.Round(p.Y, 6).ToString(CultureInfo.InvariantCulture);
                        if (!seen.Add(key)) continue;
                        try
                        {
                            double dz = c.FindElevationAtXY(p.X, p.Y) - b.FindElevationAtXY(p.X, p.Y);
                            col.Add(new Point3d(p.X, p.Y, dz));
                        }
                        catch (System.Exception) { outside++; }
                    }
                }
                foreach (ObjectId sid0 in civ.GetSurfaceIds())
                    if (((Autodesk.Civil.DatabaseServices.Surface)tr.GetObject(sid0, OpenMode.ForRead)).Name == dzName)
                        throw new InvalidOperationException("surface " + dzName + " exists");
                var dzId = Autodesk.Civil.DatabaseServices.TinSurface.Create(db, dzName);
                var dzs = (Autodesk.Civil.DatabaseServices.TinSurface)tr.GetObject(dzId, OpenMode.ForWrite);
                dzs.Layer = v.Layer;
                double? maxEdge = J.DN(a, "max_triangle_length");
                if (maxEdge.HasValue && maxEdge.Value > 0)
                {
                    dzs.BuildOptions.UseMaximumTriangleLength = true;
                    dzs.BuildOptions.MaximumTriangleLength = maxEdge.Value;
                }
                dzs.AddVertices(col);
                dzs.Rebuild();
                var dzStats = SurfaceStats(dzs);
                dzStats["vertices_outside_overlap"] = outside;
                outp["dz_surface"] = dzStats;
                double interval = J.D(a, "contour_interval", 0.0);
                if (interval > 0)
                {
                    Cad.EnsureLayer(db, tr, "VKM_DZ_ISOLINES", 1);
                    Cad.EnsureLayer(db, tr, "VKM_DZ_ISOLINES_MAJOR", 1);
                    var ids = ContoursAtMultiples(dzs, interval);
                    outp["isolines"] = PolylineNodes(tr, ids, "VKM_DZ_ISOLINES", "VKM_DZ_ISOLINES_MAJOR", J.D(a, "contour_major", 0.0), out int _);
                }
                if (J.B(a, "export_triangles", true))
                {
                    var tris = new JsonArray();
                    int n = 0, cap = (int)J.D(a, "max_triangles_export", 200000);
                    foreach (Autodesk.Civil.DatabaseServices.TinSurfaceTriangle t in dzs.GetTriangles(false))
                    {
                        if (++n > cap) break;
                        tris.Add(new JsonArray(J.Pt(t.Vertex1.Location), J.Pt(t.Vertex2.Location), J.Pt(t.Vertex3.Location)));
                    }
                    outp["dz_triangles_export"] = tris;
                }
                tr.Commit();
                return outp;
            }
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        public static JsonNode AlignmentProfile(Document doc, JsonObject a)
        {
            var db = doc.Database;
            var civ = Civil(db);
            string name = J.S(a, "name", "VKM_LINE");
            double step = J.D(a, "station_interval", 10.0);
            if (step <= 0) throw new ArgumentException("station_interval must be positive");
            var coords = J.A(a, "polyline").Select(n => J.P(n)).ToList();
            if (coords.Count < 2) throw new ArgumentException("polyline needs at least two points");
            using (var tr = db.TransactionManager.StartTransaction())
            {
                var ms = Cad.ModelSpace(db, tr);
                var layerId = Cad.EnsureLayer(db, tr, J.S(a, "layer", "VKM_ALIGNMENT"), 4);
                var pl = Cad.Pline2d(coords, false, 0);
                var plId = Cad.Add(ms, tr, pl, J.S(a, "layer", "VKM_ALIGNMENT"));
                var opt = new Autodesk.Civil.DatabaseServices.PolylineOptions
                {
                    PlineId = plId, AddCurvesBetweenTangents = false, EraseExistingEntities = true
                };
                var aid = Autodesk.Civil.DatabaseServices.Alignment.Create(civ, opt, name, ObjectId.Null, layerId,
                    civ.Styles.AlignmentStyles[0], civ.Styles.LabelSetStyles.AlignmentLabelSetStyles[0]);
                var al = (Autodesk.Civil.DatabaseServices.Alignment)tr.GetObject(aid, OpenMode.ForRead);
                var profiles = new List<Tuple<string, Autodesk.Civil.DatabaseServices.Profile>>();
                foreach (var sn in J.A(a, "surfaces"))
                {
                    string surf = sn.GetValue<string>();
                    var sid = SurfaceId(civ, tr, surf);
                    var pid = Autodesk.Civil.DatabaseServices.Profile.CreateFromSurface(name + " - " + surf, aid, sid, layerId,
                        civ.Styles.ProfileStyles[0], civ.Styles.LabelSetStyles.ProfileLabelSetStyles[0]);
                    profiles.Add(Tuple.Create(surf, (Autodesk.Civil.DatabaseServices.Profile)tr.GetObject(pid, OpenMode.ForRead)));
                }
                var stations = new List<double>();
                for (double st = al.StartingStation; st < al.EndingStation - 1e-9; st += step) stations.Add(st);
                stations.Add(al.EndingStation);
                var rows = new JsonArray();
                foreach (var st in stations)
                {
                    double x = 0, y = 0;
                    al.PointLocation(st, 0.0, ref x, ref y);
                    var row = new JsonObject { ["station"] = J.R(st), ["x"] = J.R(x), ["y"] = J.R(y) };
                    foreach (var pr in profiles)
                    {
                        try { row[pr.Item1] = J.R(pr.Item2.ElevationAt(st)); }
                        catch (System.Exception) { row[pr.Item1] = null; }
                    }
                    rows.Add(row);
                }
                var outp = new JsonObject
                {
                    ["alignment"] = name, ["length"] = J.R(al.Length), ["start_station"] = J.R(al.StartingStation),
                    ["end_station"] = J.R(al.EndingStation), ["station_interval"] = step, ["samples"] = rows,
                    ["profiles"] = J.ToNode(profiles.Select(p => p.Item2.Name).ToList())
                };
                var insert = J.A(a, "profile_view_insert");
                if (insert.Count >= 2)
                {
                    var ip = new Point3d(insert[0].GetValue<double>(), insert[1].GetValue<double>(), 0);
                    var pv = Autodesk.Civil.DatabaseServices.ProfileView.Create(aid, ip, name + " PV",
                        civ.Styles.ProfileViewBandSetStyles[0], civ.Styles.ProfileViewStyles[0]);
                    outp["profile_view"] = name + " PV";
                }
                tr.Commit();
                return outp;
            }
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        public static JsonNode List(Document doc, JsonObject a)
        {
            var db = doc.Database;
            var civ = Civil(db);
            var surfaces = new JsonArray();
            var alignments = new JsonArray();
            var groups = new JsonArray();
            using (var tr = db.TransactionManager.StartTransaction())
            {
                foreach (ObjectId id in civ.GetSurfaceIds())
                    surfaces.Add(SurfaceStats((Autodesk.Civil.DatabaseServices.Surface)tr.GetObject(id, OpenMode.ForRead)));
                foreach (ObjectId id in civ.GetAlignmentIds())
                {
                    var al = (Autodesk.Civil.DatabaseServices.Alignment)tr.GetObject(id, OpenMode.ForRead);
                    alignments.Add(new JsonObject { ["name"] = al.Name, ["length"] = J.R(al.Length) });
                }
                foreach (ObjectId id in civ.PointGroups)
                {
                    var pg = (Autodesk.Civil.DatabaseServices.PointGroup)tr.GetObject(id, OpenMode.ForRead);
                    groups.Add(new JsonObject { ["name"] = pg.Name, ["points"] = (int)pg.PointsCount });
                }
                tr.Commit();
            }
            return new JsonObject
            {
                ["surfaces"] = surfaces, ["alignments"] = alignments, ["point_groups"] = groups,
                ["cogo_points"] = (int)civ.CogoPoints.Count
            };
        }
    }
}

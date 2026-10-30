"""Public publishing boundaries and the synthetic demo's browser data contract."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_site


ROOT = Path(__file__).resolve().parents[1]


class PublicSiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)

    def fixture(self):
        root = self.directory / "source"
        sources = ["site/" + name for name in build_site.SITE_FILES]
        sources += ["tools/web/static/" + name for name in (*build_site.WORKBENCH_FILES, "index.html")]
        for name in sources:
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        return root

    def test_build_only_publishes_explicit_static_inputs(self):
        root = self.fixture()
        private = "PRIVATE_FIXTURE_NOT_FOR_PUBLICATION"
        for name in ("config.json", ".share/memory/event.json", ".local/interaction.json",
                     ".git/config", "site/draft.json"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(private, encoding="utf-8")
        output = self.directory / "public"
        with patch.object(build_site, "ROOT", root):
            build_site.build(output)
            build_site.build(output)  # An ordinary rebuild remains possible.
        self.assertTrue((output / ".nojekyll").is_file())
        self.assertEqual((output / "demo/app.js").read_bytes(),
                         (ROOT / "tools/web/static/app.js").read_bytes())
        self.assertEqual((output / "demo/api.js").read_bytes(),
                         (ROOT / "site/demo/api.js").read_bytes())
        for path in output.rglob("*"):
            if path.is_file():
                self.assertNotIn(private.encode(), path.read_bytes(), str(path))
        self.assertFalse((output / "config.json").exists())
        self.assertFalse((output / "draft.json").exists())
        html = (output / "demo/index.html").read_text(encoding="utf-8")
        self.assertIn("DEMO DATA", html)
        self.assertIn('href="../"', html)
        self.assertIn('href="demo.css"', html)
        self.assertNotIn("数据来自现有工作区", html)

    def test_symlinked_input_cannot_publish_a_private_file(self):
        root = self.fixture()
        private = self.directory / "private.js"
        private.write_text("private fixture", encoding="utf-8")
        source = root / "site/main.js"
        source.unlink()
        source.symlink_to(private)
        with patch.object(build_site, "ROOT", root), self.assertRaisesRegex(ValueError, "symlink"):
            build_site.build(self.directory / "public")

    def test_reused_output_rejects_unrelated_files_without_deleting_them(self):
        root = self.fixture()
        output = self.directory / "public"
        output.mkdir()
        (output / ".nojekyll").touch()
        private = output / "private.json"
        private.write_text("keep this local file", encoding="utf-8")
        with patch.object(build_site, "ROOT", root), self.assertRaises(ValueError):
            build_site.build(output)
        self.assertEqual(private.read_text(encoding="utf-8"), "keep this local file")

    def test_symlinked_output_cannot_overwrite_another_directory(self):
        target = self.directory / "existing"
        target.mkdir()
        output = self.directory / "public"
        output.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "symlink"):
            build_site.build(output)
        self.assertEqual(list(target.iterdir()), [])

    def test_demo_references_and_synthetic_notices_are_complete(self):
        data = json.loads((ROOT / "site/demo/data.json").read_text(encoding="utf-8"))
        self.assertIn("合成", data["notice"])
        self.assertEqual(len(data["overview"]["projects"]), 3)
        self.assertTrue(data["timeline"]["project_mode"] == "knowledge")
        for project in data["overview"]["projects"]:
            timeline = data["project_timelines"][project["name"]]
            self.assertEqual(timeline["projects"][0]["name"], project["name"])
            for doc in project["documents"]:
                path = doc if isinstance(doc, str) else doc["path"]
                self.assertIn(path, data["document_previews"])
        for task, timeline in data["task_timelines"].items():
            self.assertIn(task, data["handoffs"])
            self.assertIn(task, data["reviews"])
            self.assertFalse(data["handoffs"][task]["handoff"]["dispatched"])
            for event in timeline["projects"][0]["milestones"]:
                self.assertIn(event["event_id"], data["events"])

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser-module regression checks")
    def test_static_client_filters_without_accessing_live_api(self):
        module = self.directory / "api.mjs"
        module.write_bytes((ROOT / "site/demo/api.js").read_bytes())
        script = """
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const raw=JSON.parse(readFileSync(process.argv[2],'utf8'));
let reads=0;
globalThis.fetch=async url=>{
  reads++;
  assert.equal(new URL(url).pathname.endsWith('/data.json'),true);
  return {ok:true,json:async()=>structuredClone(raw)};
};
const {api}=await import(pathToFileURL(process.argv[1]).href);
const overview=await api('overview');
overview.projects.length=0;
assert.equal((await api('overview')).projects.length,3);
for(const project of raw.overview.projects) {
  assert.equal((await api('timeline',{project:project.name})).projects[0].name,project.name);
  const allowed=new Set(project.documents.map(d=>typeof d==='string'?d:d.path));
  const docs=await api('documents',{business:project.name});
  assert.ok(docs.items.length>0 && docs.items.every(doc=>allowed.has(doc.path)));
}
const document=raw.documents[0];
assert.ok((await api('documents',{q:'  '+document.title+'  '})).items.some(d=>d.path===document.path));
assert.equal((await api('documents',{q:'no-such-synthetic-document'})).items.length,0);
const task=Object.keys(raw.task_timelines)[0];
const handoff=await api('handoff',{task,to_agent:'kimi'});
assert.deepEqual(handoff.handoff,{state:'prepared',to_agent:'kimi',dispatched:false});
for(const max_chars of [1000,2000,4000,12000]) {
  const packet=await api('review',{task,limit:1,max_chars});
  assert.equal(packet.state,'prepared');
  assert.ok(packet.candidates.length<=1);
  assert.ok([...JSON.stringify(packet)].length<=max_chars);
  assert.equal(packet.budget.used_chars,[...JSON.stringify(packet)].length);
  assert.equal(packet.omitted,packet.current_total-packet.candidates.length);
}
await assert.rejects(api('review',{task,limit:0}));
await assert.rejects(api('handoff',{task,to_agent:'unknown-agent'}));
await assert.rejects(api('documents',{business:'missing'}));
await assert.rejects(api('restart'));
await assert.rejects(api('document',{path:'../../config.json'}));
const controller=new AbortController();controller.abort();
await assert.rejects(api('cluster',{}, {signal:controller.signal}),{name:'AbortError'});
assert.equal(reads,1);
console.log('static client: filters, budgets, abort, isolation and no live fallback verified');
"""
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", script, str(module),
             str(ROOT / "site/demo/data.json")],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is required for browser-module regression checks")
    def test_local_client_preserves_read_only_http_and_abort(self):
        module = self.directory / "api.mjs"
        module.write_bytes((ROOT / "tools/web/static/api.js").read_bytes())
        script = """
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
let seen;
globalThis.fetch=async(url,options)=>{seen={url,options};return {ok:true,json:async()=>({ok:true})};};
const {api}=await import(pathToFileURL(process.argv[1]).href);
const controller=new AbortController();
assert.deepEqual(await api('documents',{q:'hello & 中文'}, {signal:controller.signal}),{ok:true});
assert.equal(new URL(seen.url,'https://example.test/demo/').pathname,'/demo/api/documents');
assert.equal(new URL(seen.url,'https://example.test/demo/').searchParams.get('q'),'hello & 中文');
assert.equal(seen.options.cache,'no-store');
assert.equal(seen.options.signal,controller.signal);
assert.equal(seen.options.method,undefined);
globalThis.fetch=async()=>({ok:false,json:async()=>({error:'expected read error'})});
await assert.rejects(api('cluster'),/expected read error/);
"""
        result = subprocess.run(
            [shutil.which("node"), "--input-type=module", "-e", script, str(module)],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()

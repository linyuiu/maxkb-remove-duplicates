const { PGlite } = await import("@electric-sql/pglite");
const { vector } = await import("@electric-sql/pglite-pgvector");
let input = "";
for await (const chunk of process.stdin) input += chunk;
const p = JSON.parse(input),
  db = new PGlite({ extensions: { vector } });
await db.exec(
  `CREATE EXTENSION vector; CREATE TABLE embedding(id text PRIMARY KEY,knowledge_id uuid,source_type text,paragraph_id text,document_id text,embedding vector);`,
);
const selected = [
  "00000000-0000-0000-0000-000000000001",
  "00000000-0000-0000-0000-000000000002",
];
for (const [i, row] of p.rows.entries())
  await db.query("INSERT INTO embedding VALUES ($1,$2,$3,$4,$5,$6::vector)", [
    row.embedding_id,
    selected[i % 2],
    "1",
    row.paragraph_id,
    row.document_id,
    JSON.stringify(row.vector),
  ]);
await db.exec(
  `INSERT INTO embedding VALUES ('0-outside','00000000-0000-0000-0000-000000000003','1','leak','leak','[1,0,0,0]'),('z-outside','00000000-0000-0000-0000-000000000003','1','leak2','leak2','[1,0,0,0]'),('0-problem','00000000-0000-0000-0000-000000000001','0','problem','problem','[1,0,0,0]');`,
);
const names = ["knowledge_ids", "start", "end", "exclude_doc", "threshold"];
const sql = p.sql.replace(
  /%\((\w+)\)s/g,
  (_, name) => "$" + (names.indexOf(name) + 1),
);
const pairs = new Map();
let after = null;
while (true) {
  const { rows: anchors } = await db.query(
    `SELECT id FROM embedding WHERE knowledge_id=ANY($1::uuid[]) AND source_type='1' AND ($2::text IS NULL OR id>$2) ORDER BY id LIMIT $3`,
    [selected, after, p.batch_size],
  );
  if (!anchors.length) break;
  const end = anchors.at(-1).id;
  const { rows } = await db.query(sql, [
    selected,
    after,
    end,
    p.exclude_doc,
    p.threshold,
  ]);
  for (const row of rows) {
    const key = row.left_id + ":" + row.right_id,
      old = pairs.get(key);
    if (!old || row.similarity > old.similarity) pairs.set(key, row);
  }
  after = end;
}
process.stdout.write(JSON.stringify([...pairs.values()]));
await db.close();

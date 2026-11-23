// tsc input while no helper is TypeScript yet: needs ESM and the node types.
import { basename } from "node:path";

const dir: string = basename(import.meta.dirname);
console.log(dir);

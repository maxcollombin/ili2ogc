CREATE TABLE "point" (
    "id" text UNIQUE NOT NULL
);
-- NOTE (point): [SQL-GEOM-NO-CRS] Pos: no resolved CRS (!!@CRS meta-attribute) - provide the geometry base model via --repo
CREATE TABLE "multipoint" (
    "id" text UNIQUE NOT NULL
);
-- NOTE (multipoint): [SQL-GEOM-NO-CRS] Pos: no resolved CRS (!!@CRS meta-attribute) - provide the geometry base model via --repo
CREATE TABLE "way" (
    "id" text UNIQUE NOT NULL
);
-- NOTE (way): [SQL-GEOM-NO-CRS] Geom: no resolved CRS (!!@CRS meta-attribute) - provide the geometry base model via --repo
CREATE TABLE "directedway" (
    "id" text UNIQUE NOT NULL
);
-- NOTE (directedway): [SQL-GEOM-NO-CRS] Geom: no resolved CRS (!!@CRS meta-attribute) - provide the geometry base model via --repo
CREATE TABLE "multiway" (
    "id" text UNIQUE NOT NULL
);
-- NOTE (multiway): [SQL-GEOM-NO-CRS] Geom: no resolved CRS (!!@CRS meta-attribute) - provide the geometry base model via --repo
CREATE TABLE "zone" (
    "id" text UNIQUE NOT NULL
);
-- NOTE (zone): [SQL-GEOM-NO-CRS] Geom: no resolved CRS (!!@CRS meta-attribute) - provide the geometry base model via --repo

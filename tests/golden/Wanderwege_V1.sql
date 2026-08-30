CREATE TABLE "wegabschnitt" (
    "id" text UNIQUE NOT NULL,
    "bezeichnung" varchar(40) NOT NULL,
    "kategorie" text NOT NULL,
    "belagsart" varchar(20)
);
CREATE TABLE "wegweiser" (
    "id" text UNIQUE NOT NULL,
    "standort" varchar(40) NOT NULL,
    "hoehe" integer,
    "wegabschnitt" text
);
ALTER TABLE "wegweiser" ADD CONSTRAINT fk_wegweiser_wegabschnitt FOREIGN KEY ("wegabschnitt") REFERENCES "wegabschnitt" ("id");
CREATE VIEW "wegabschnitt_mitwegweiser" AS
    SELECT
        "wegabschnitt"."bezeichnung" AS "bezeichnung",
        "wegabschnitt"."kategorie" AS "kategorie",
        "wegabschnitt"."belagsart" AS "belagsart"
    FROM "wegabschnitt" "wegabschnitt"
    WHERE (EXISTS (SELECT 1 FROM "wegweiser" "v1" WHERE "v1"."wegabschnitt" = "wegabschnitt"."id"));

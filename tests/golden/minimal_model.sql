CREATE TABLE "person" (
    "id" text UNIQUE NOT NULL,
    "name" text NOT NULL,
    "birthyear" integer,
    "kind" text,
    "employer" text
);
CREATE TABLE "company" (
    "id" text UNIQUE NOT NULL,
    "name" text NOT NULL
);
ALTER TABLE "person" ADD CONSTRAINT fk_person_employer FOREIGN KEY ("employer") REFERENCES "company" ("id");

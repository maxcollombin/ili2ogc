# interlis-runtime

Runtime Python pour INTERLIS basé sur une grammaire ANTLR4.

Ce dépôt contient actuellement :
- la génération du lexer/parser Python depuis la grammaire INTERLIS maintenue dans `vendor/interlis-antlr4`
- le runtime ANTLR Python généré dans `src/interlis/antlr`
- les bases du futur modèle objet INTERLIS en Python

## Installation

```sh
uv sync
```

## Génération du parser ANTLR

La grammaire INTERLIS est incluse comme sous-module Git :

```sh
git submodule update --init --recursive
```

## Génération du lexer

```sh
uv run --env-file .env antlr4 \
    -Dlanguage=Python3 \
    -visitor \
    -no-listener \
    -Xexact-output-dir \
    -o src/interlis/antlr \
    vendor/interlis-antlr4/InterlisLexer.g4
```

## Génération du parser et du vistor

```sh
uv run --env-file .env antlr4 \
    -Dlanguage=Python3 \
    -visitor \
    -no-listener \
    -Xexact-output-dir \
    -lib src/interlis/antlr \
    -o src/interlis/antlr \
    vendor/interlis-antlr4/InterlisParser.g4
```

## Prompt de suivi:

    Objectif : construire une implémentation Python du métamodèle INTERLIS à partir de la grammaire ANTLR et d'IlisMeta16.

    Étapes :

    Uniformiser grammar-metamodel-mapping.yml en utilisant systématiquement les concepts target, construction, parent, identifier et discriminator à la place des définitions hétérogènes actuelles.
        Compléter le mapping de toutes les règles ANTLR vers les éléments correspondants d'IlisMeta16.
        À partir de l'export UML d'IlisMeta16, construire la description complète du métamodèle (classes, attributs, héritage, associations, cardinalités, énumérations, contraintes).
        Répartir les informations dans des fichiers dédiés :
            grammar-metamodel-mapping.yml : ANTLR → métamodèle ;
            ilismeta16-model.yml : description complète du métamodèle ;
            python-bindings.yml : correspondance métamodèle → implémentation Python.
        Utiliser ces fichiers comme source unique pour générer automatiquement le métamodèle Python et, à terme, le visitor ANTLR.

Je simplifierais toutefois légèrement l'architecture par rapport à ce que j'avais proposé précédemment, puisque tu ne vises que Python.

Je garderais seulement trois fichiers :

- grammar-metamodel-mapping.yml : décrit comment chaque règle ANTLR construit un élément du métamodèle (target, construction, etc.).
- ilismeta16-model.yml : description exhaustive d'IlisMeta16, générée ou dérivée de l'export UML.
- python-bindings.yml : indique uniquement comment chaque classe du métamodèle est implémentée en Python (module, classe, éventuellement types Python spécifiques).

Je ne créerais pas de canonical-model.yml dans un premier temps. Tu pourras toujours l'introduire plus tard si tu souhaites rendre ton projet indépendant d'IlisMeta16 ou cibler d'autres représentations (SQL, JSON Schema, etc.). Pour l'instant, rester fidèle au métamodèle officiel te permettra d'avancer plus rapidement vers un métamodèle Python exécutable.

__NOTE:__
Structurer ilismeta16-model.yml comme un véritable modèle UML.

```ỳml
classes:

  Class:

    extends: Type

    attributes:

      Kind:
        type: ClassKind

      EmbeddedRoleTransfer:
        type: BOOLEAN

    associations:

      attributes:

        association: ClassAttr

        role: ClassAttribute

      parameters:

        association: ClassParam

        role: ClassParameter
```


## Architecture:

InterlisParser.g4
        │
        ▼
grammar-metamodel-mapping.yml
        │
        ▼
ilismeta16-model.yml
        │
        ▼
python-bindings.yml
        │
        ▼
Métamodèle Python généré

## Délégation des rôles


```sh
mapping/
│
├── grammar-metamodel-mapping.yml
│
├── ilismeta16-classes.yml
│
├── ilismeta16-associations.yml
│
└── python-bindings.yml
```

Le premier répond à :

    "Quelle règle de grammaire produit quel concept métier ?"

Le second :

    "Quels sont les attributs et héritages de chaque concept ?"

Le troisième :

    "Comment les relations entre objets sont représentées ?"

Le quatrième :

    "Comment produire les classes Python ?"


## TODOS    

### Valider les règles suivantes en fonction de l'UML

awk '
/^[a-zA-Z0-9_]+:$/ {rule=$1}
/CHECK/ {print rule}
' docs/grammar-metamodel-mapping.yml

attrType:
referenceAttr:
restrictedClassOrAssRef:
classOrAssociationRef:
restrictedStructureRef:
restrictedClassOrStructureRef:
type:
baseType:
coordinateType:
rotationDef:
oIDType:
blackboxType:
classType:
attributeType:
lineType:
lineForm:
lineFormType:
controlPoints:
intersectionDef:
lineFormTypeDef:
constraintDef:
mandatoryConstraint:
plausibilityConstraint:
existenceConstraint:
uniquenessConstraint:
globalUniqueness:
localUniqueness:
uniqueEl:
setConstraint:
metaDataBasketDef:
expression:
predicate:
functionCall:
argument:
objectOrAttributePath:
functionDecl:
functionDef:
argumentDef:
parameterDef:
runTimeParameterDef:
unitDef:
derivedUnit:
composedUnit:
unitRef:
viewDef:
viewRef:
formationDef:
projection:
join:
union:
aggregation:
inspection:
renamedViewableRef:
viewableRef:
baseExtensionDef:
selection:
viewAttributes:
graphicDef:
drawingRule:
condSignParamAssignment:

### Améliorer le regroupement par sections et sous-sections en fonction de l'UML


a prochaine étape est de créer :

scripts/check_mapping.py

Son rôle sera :

    charger :
        docs/grammar-metamodel-mapping.yml
        docs/ilismeta16-classes.yml
        docs/ilismeta16-datatypes.yml
        docs/ilismeta16-associations.yml
        docs/ilismeta16-extensions.yml

    construire un index global :

{
    "INTERLIS.METAOBJECT": "Class",
    "IlisMeta16.ModelData.MetaElement": "Class",
    "INTERLIS.LENGTH": "Class",
    ...
}

    analyser chaque entrée du mapping :

Exemple :

MetaObject:
  metamodel:
    class: INTERLIS.METAOBJECT

→

OK INTERLIS.METAOBJECT (Class)

Mais :

classTypeRef:
  grammar:
    rule: classTypeRef

→

WARNING: no UML target defined

et :

ReferenceType:
  metamodel:
    class: ReferenceType

→

WARNING: ReferenceType absent du XMI
INFO: rechercher dans ilismeta16-extensions.yml


Je travaille sur un runtime INTERLIS en Python (parsing .ili/.xtf vers SQL,
JSON Schema, JSON-FG, symbologie cartographique). Le pipeline :

  grammaire ANTLR4 (Interlis.g4) --génère--> src/interlis/antlr/InterlisParser.py
  docs/uml/IlisMeta16.xmi --scripts/extract_ilismeta16.py--> mappings/ilismeta16-{classes,datatypes,
      associations,enumerations,extensions,model}.yml  (métamodèle IlisMeta16 canonique)
  mappings/grammar-metamodel-mapping.yml : associe chaque règle de grammaire à une
      métaclasse IlisMeta16 (+ discriminant Kind si plusieurs règles partagent une cible,
      ex. classDef/structureDef/associationDef -> IlisMeta16.ModelData.Class)
  mappings/ilismeta16-kind-values.yml : valeurs légales des attributs Kind/Order/
      Strongness/etc. absentes du XMI (extraites manuellement de la spec officielle
      INTERLIS 2 Metamodel edition 2022-06-17), généré par scripts/generate_kind_values.py,
      validé par scripts/verify_kind_values.py (état actuel : 21/21 OK, aucune entrée
      manquante ni orpheline)
  mappings/python-bindings.yml (squelette généré par
      scripts/generate_python_bindings_skeleton.py, via introspection réelle des
      *Context d'InterlisParser.py) : doit à terme permettre de construire, depuis
      l'AST ANTLR, un métamodèle IlisMeta16 exécutable en pure Python (ModelBuilder),
      sans dépendance à ili2c à l'exécution.

scripts/check_mapping.py valide grammar-metamodel-mapping.yml contre les fichiers
ilismeta16-*.yml (état actuel : 121 règles, 14 avec cible directe, 14 OK, 0 manquant,
80 "without_target" restant à traiter).

IMPORTANT - grammar-metamodel-mapping.yml est organisé en 9 sections numérotées
(commentaires `# ==...==` suivis de `# 01_root`, etc., PAS des clés YAML imbriquées -
les 121 règles restent au premier niveau du dict) :
  01_root (1), 02_packages (5), 03_classes_and_structures (6), 04_attributes (10),
  05_associations (4), 06_types (37), 07_constraints (28), 08_functions_units (10),
  09_views_graphics (20)
Le détail complet règle-par-règle par section est disponible via
scripts/extract_sections.py (déjà écrit, à relancer si besoin). python-bindings.yml
doit refléter le même découpage en sections (mêmes en-têtes, même ordre de règles)
pour rester lisible et synchronisé avec grammar-metamodel-mapping.yml - actuellement
generate_python_bindings_skeleton.py écrit un yaml.dump() plat qui perd cet ordre et
ces commentaires, à corriger (écriture manuelle section par section plutôt qu'un dump
global).

PROBLÈME EN SUSPENS le plus important : plusieurs règles de grammar-metamodel-mapping.yml
portent un commentaire "# CHECK UML" signalant que la correspondance grammaire->métamodèle
n'est pas encore tranchée. Exemple pour attrType :
    metamodel:
      model: IlisMeta16
      # CHECK UML:
      # Dispatcher between primitive types, reference attributes and line types.
      # No direct IlisMeta16 instance expected.
    semantics:
      construction:
        kind: Container
Il faut lister TOUTES les règles marquées "CHECK UML" (attrType, restrictedClassOrAssRef,
et d'autres - liste exhaustive à établir), comprendre pour chacune si :
  (a) c'est un vrai "dispatcher"/règle de grammaire purement syntaxique sans instance
      IlisMeta16 propre (kind: Container est alors correct, rien à ajouter), ou
  (b) elle devrait en réalité avoir une cible IlisMeta16 précise qu'on a juste pas
      encore déterminée.

Prochaines étapes, dans l'ordre :
1. Lister exhaustivement les règles "CHECK UML" (grep sur le commentaire dans le
   fichier) et trancher (a) vs (b) pour chacune, en s'appuyant sur ilismeta16-classes/
   datatypes/associations.yml et la spec officielle INTERLIS 2 Metamodel déjà utilisée
   pour kind-values.yml.
2. Corriger generate_python_bindings_skeleton.py pour préserver les sections/l'ordre
   de grammar-metamodel-mapping.yml dans python-bindings.yml.
3. Étendre check_mapping.py pour valider la *valeur* du discriminant (pas seulement
   l'attribut) contre kind-values.yml.
4. Traiter les 80 règles "without_target" restantes.
5. Remplir construction.attribute_bindings ("TODO" actuellement) dans python-bindings.yml,
   en commençant par le cœur du métamodèle (03_classes_and_structures, 04_attributes,
   05_associations) avant les sections périphériques (types, contraintes, fonctions,
   vues/graphisme).
6. Concevoir le ModelBuilder qui consomme python-bindings.yml + l'AST pour produire des
   instances Python exécutables du métamodèle IlisMeta16.

Demande les commandes shell/Python nécessaires pour inspecter l'état actuel (liste des
CHECK UML, structure ast.fields déjà générée pour une règle donnée, contenu détaillé
d'une section de grammar-metamodel-mapping.yml) avant de proposer des corrections.
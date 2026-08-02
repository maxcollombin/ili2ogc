# Generated from InterlisParser.g4 by ANTLR 4.13.2
from antlr4 import *
if "." in __name__:
    from .InterlisParser import InterlisParser
else:
    from InterlisParser import InterlisParser

# This class defines a complete generic visitor for a parse tree produced by InterlisParser.

class InterlisParserVisitor(ParseTreeVisitor):

    # Visit a parse tree produced by InterlisParser#interlis2def.
    def visitInterlis2def(self, ctx:InterlisParser.Interlis2defContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#modeldef.
    def visitModeldef(self, ctx:InterlisParser.ModeldefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#topicDef.
    def visitTopicDef(self, ctx:InterlisParser.TopicDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#definitions.
    def visitDefinitions(self, ctx:InterlisParser.DefinitionsContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#topicRef.
    def visitTopicRef(self, ctx:InterlisParser.TopicRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#genericRef.
    def visitGenericRef(self, ctx:InterlisParser.GenericRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#classDef.
    def visitClassDef(self, ctx:InterlisParser.ClassDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#structureDef.
    def visitStructureDef(self, ctx:InterlisParser.StructureDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#classRef.
    def visitClassRef(self, ctx:InterlisParser.ClassRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#classOrStructureDef.
    def visitClassOrStructureDef(self, ctx:InterlisParser.ClassOrStructureDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#structureRef.
    def visitStructureRef(self, ctx:InterlisParser.StructureRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#classOrStructureRef.
    def visitClassOrStructureRef(self, ctx:InterlisParser.ClassOrStructureRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#attributeDef.
    def visitAttributeDef(self, ctx:InterlisParser.AttributeDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#attrTypeDef.
    def visitAttrTypeDef(self, ctx:InterlisParser.AttrTypeDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#attrType.
    def visitAttrType(self, ctx:InterlisParser.AttrTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#referenceAttr.
    def visitReferenceAttr(self, ctx:InterlisParser.ReferenceAttrContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#restrictedClassOrAssRef.
    def visitRestrictedClassOrAssRef(self, ctx:InterlisParser.RestrictedClassOrAssRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#classOrAssociationRef.
    def visitClassOrAssociationRef(self, ctx:InterlisParser.ClassOrAssociationRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#restrictedStructureRef.
    def visitRestrictedStructureRef(self, ctx:InterlisParser.RestrictedStructureRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#restrictedClassOrStructureRef.
    def visitRestrictedClassOrStructureRef(self, ctx:InterlisParser.RestrictedClassOrStructureRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#associationDef.
    def visitAssociationDef(self, ctx:InterlisParser.AssociationDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#associationRef.
    def visitAssociationRef(self, ctx:InterlisParser.AssociationRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#roleDef.
    def visitRoleDef(self, ctx:InterlisParser.RoleDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#cardinality.
    def visitCardinality(self, ctx:InterlisParser.CardinalityContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#domainDef.
    def visitDomainDef(self, ctx:InterlisParser.DomainDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#type.
    def visitType(self, ctx:InterlisParser.TypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#domainRef.
    def visitDomainRef(self, ctx:InterlisParser.DomainRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#baseType.
    def visitBaseType(self, ctx:InterlisParser.BaseTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#constant.
    def visitConstant(self, ctx:InterlisParser.ConstantContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#textType.
    def visitTextType(self, ctx:InterlisParser.TextTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#textConst.
    def visitTextConst(self, ctx:InterlisParser.TextConstContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#enumerationType.
    def visitEnumerationType(self, ctx:InterlisParser.EnumerationTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#enumTreeValueType.
    def visitEnumTreeValueType(self, ctx:InterlisParser.EnumTreeValueTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#enumeration.
    def visitEnumeration(self, ctx:InterlisParser.EnumerationContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#enumElement.
    def visitEnumElement(self, ctx:InterlisParser.EnumElementContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#enumerationConst.
    def visitEnumerationConst(self, ctx:InterlisParser.EnumerationConstContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#alignmentType.
    def visitAlignmentType(self, ctx:InterlisParser.AlignmentTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#booleanType.
    def visitBooleanType(self, ctx:InterlisParser.BooleanTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#numeric.
    def visitNumeric(self, ctx:InterlisParser.NumericContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#numericType.
    def visitNumericType(self, ctx:InterlisParser.NumericTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#refSys.
    def visitRefSys(self, ctx:InterlisParser.RefSysContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#decConst.
    def visitDecConst(self, ctx:InterlisParser.DecConstContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#numericConst.
    def visitNumericConst(self, ctx:InterlisParser.NumericConstContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#formattedType.
    def visitFormattedType(self, ctx:InterlisParser.FormattedTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#formatDef.
    def visitFormatDef(self, ctx:InterlisParser.FormatDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#baseAttrRef.
    def visitBaseAttrRef(self, ctx:InterlisParser.BaseAttrRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#formattedConst.
    def visitFormattedConst(self, ctx:InterlisParser.FormattedConstContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#dateTimeType.
    def visitDateTimeType(self, ctx:InterlisParser.DateTimeTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#coordinateType.
    def visitCoordinateType(self, ctx:InterlisParser.CoordinateTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#rotationDef.
    def visitRotationDef(self, ctx:InterlisParser.RotationDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#contextDef.
    def visitContextDef(self, ctx:InterlisParser.ContextDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#oIDType.
    def visitOIDType(self, ctx:InterlisParser.OIDTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#blackboxType.
    def visitBlackboxType(self, ctx:InterlisParser.BlackboxTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#classType.
    def visitClassType(self, ctx:InterlisParser.ClassTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#attributeType.
    def visitAttributeType(self, ctx:InterlisParser.AttributeTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#classConst.
    def visitClassConst(self, ctx:InterlisParser.ClassConstContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#attributePathConst.
    def visitAttributePathConst(self, ctx:InterlisParser.AttributePathConstContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#lineType.
    def visitLineType(self, ctx:InterlisParser.LineTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#lineForm.
    def visitLineForm(self, ctx:InterlisParser.LineFormContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#lineFormType.
    def visitLineFormType(self, ctx:InterlisParser.LineFormTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#controlPoints.
    def visitControlPoints(self, ctx:InterlisParser.ControlPointsContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#intersectionDef.
    def visitIntersectionDef(self, ctx:InterlisParser.IntersectionDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#lineFormTypeDef.
    def visitLineFormTypeDef(self, ctx:InterlisParser.LineFormTypeDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#unitDef.
    def visitUnitDef(self, ctx:InterlisParser.UnitDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#derivedUnit.
    def visitDerivedUnit(self, ctx:InterlisParser.DerivedUnitContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#composedUnit.
    def visitComposedUnit(self, ctx:InterlisParser.ComposedUnitContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#unitRef.
    def visitUnitRef(self, ctx:InterlisParser.UnitRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#metaDataBasketDef.
    def visitMetaDataBasketDef(self, ctx:InterlisParser.MetaDataBasketDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#metaDataBasketRef.
    def visitMetaDataBasketRef(self, ctx:InterlisParser.MetaDataBasketRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#metaObjectRef.
    def visitMetaObjectRef(self, ctx:InterlisParser.MetaObjectRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#parameterDef.
    def visitParameterDef(self, ctx:InterlisParser.ParameterDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#runTimeParameterDef.
    def visitRunTimeParameterDef(self, ctx:InterlisParser.RunTimeParameterDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#constraintDef.
    def visitConstraintDef(self, ctx:InterlisParser.ConstraintDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#mandatoryConstraint.
    def visitMandatoryConstraint(self, ctx:InterlisParser.MandatoryConstraintContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#plausibilityConstraint.
    def visitPlausibilityConstraint(self, ctx:InterlisParser.PlausibilityConstraintContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#existenceConstraint.
    def visitExistenceConstraint(self, ctx:InterlisParser.ExistenceConstraintContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#uniquenessConstraint.
    def visitUniquenessConstraint(self, ctx:InterlisParser.UniquenessConstraintContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#globalUniqueness.
    def visitGlobalUniqueness(self, ctx:InterlisParser.GlobalUniquenessContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#uniqueEl.
    def visitUniqueEl(self, ctx:InterlisParser.UniqueElContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#localUniqueness.
    def visitLocalUniqueness(self, ctx:InterlisParser.LocalUniquenessContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#setConstraint.
    def visitSetConstraint(self, ctx:InterlisParser.SetConstraintContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#constraintsDef.
    def visitConstraintsDef(self, ctx:InterlisParser.ConstraintsDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#expression.
    def visitExpression(self, ctx:InterlisParser.ExpressionContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#term.
    def visitTerm(self, ctx:InterlisParser.TermContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#term0.
    def visitTerm0(self, ctx:InterlisParser.Term0Context):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#term1.
    def visitTerm1(self, ctx:InterlisParser.Term1Context):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#term2.
    def visitTerm2(self, ctx:InterlisParser.Term2Context):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#predicate.
    def visitPredicate(self, ctx:InterlisParser.PredicateContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#relation.
    def visitRelation(self, ctx:InterlisParser.RelationContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#factor.
    def visitFactor(self, ctx:InterlisParser.FactorContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#objectOrAttributePath.
    def visitObjectOrAttributePath(self, ctx:InterlisParser.ObjectOrAttributePathContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#attributePath.
    def visitAttributePath(self, ctx:InterlisParser.AttributePathContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#pathEl.
    def visitPathEl(self, ctx:InterlisParser.PathElContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#associationPath.
    def visitAssociationPath(self, ctx:InterlisParser.AssociationPathContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#attributeRef.
    def visitAttributeRef(self, ctx:InterlisParser.AttributeRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#functionCall.
    def visitFunctionCall(self, ctx:InterlisParser.FunctionCallContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#argument.
    def visitArgument(self, ctx:InterlisParser.ArgumentContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#functionDecl.
    def visitFunctionDecl(self, ctx:InterlisParser.FunctionDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#functionDef.
    def visitFunctionDef(self, ctx:InterlisParser.FunctionDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#argumentDef.
    def visitArgumentDef(self, ctx:InterlisParser.ArgumentDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#argumentType.
    def visitArgumentType(self, ctx:InterlisParser.ArgumentTypeContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#viewDef.
    def visitViewDef(self, ctx:InterlisParser.ViewDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#viewRef.
    def visitViewRef(self, ctx:InterlisParser.ViewRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#formationDef.
    def visitFormationDef(self, ctx:InterlisParser.FormationDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#projection.
    def visitProjection(self, ctx:InterlisParser.ProjectionContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#join.
    def visitJoin(self, ctx:InterlisParser.JoinContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#union.
    def visitUnion(self, ctx:InterlisParser.UnionContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#aggregation.
    def visitAggregation(self, ctx:InterlisParser.AggregationContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#inspection.
    def visitInspection(self, ctx:InterlisParser.InspectionContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#renamedViewableRef.
    def visitRenamedViewableRef(self, ctx:InterlisParser.RenamedViewableRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#viewableRef.
    def visitViewableRef(self, ctx:InterlisParser.ViewableRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#baseExtensionDef.
    def visitBaseExtensionDef(self, ctx:InterlisParser.BaseExtensionDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#selection.
    def visitSelection(self, ctx:InterlisParser.SelectionContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#viewAttributes.
    def visitViewAttributes(self, ctx:InterlisParser.ViewAttributesContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#graphicDef.
    def visitGraphicDef(self, ctx:InterlisParser.GraphicDefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#graphicRef.
    def visitGraphicRef(self, ctx:InterlisParser.GraphicRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#drawingRule.
    def visitDrawingRule(self, ctx:InterlisParser.DrawingRuleContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#condSignParamAssignment.
    def visitCondSignParamAssignment(self, ctx:InterlisParser.CondSignParamAssignmentContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#signParamAssignment.
    def visitSignParamAssignment(self, ctx:InterlisParser.SignParamAssignmentContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#enumAssignment.
    def visitEnumAssignment(self, ctx:InterlisParser.EnumAssignmentContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by InterlisParser#enumRange.
    def visitEnumRange(self, ctx:InterlisParser.EnumRangeContext):
        return self.visitChildren(ctx)



del InterlisParser
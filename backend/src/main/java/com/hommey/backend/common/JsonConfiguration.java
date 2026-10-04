package com.hommey.backend.common;

import com.fasterxml.jackson.databind.cfg.CoercionAction;
import com.fasterxml.jackson.databind.cfg.CoercionInputShape;
import com.fasterxml.jackson.databind.type.LogicalType;
import org.springframework.boot.autoconfigure.jackson.Jackson2ObjectMapperBuilderCustomizer;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;

@Configuration
public class JsonConfiguration {
  @Bean
  Jackson2ObjectMapperBuilderCustomizer strictScalarTypes() {
    return builder ->
        builder.postConfigurer(
            mapper -> {
              for (var type : new LogicalType[] {LogicalType.Integer, LogicalType.Boolean}) {
                mapper
                    .coercionConfigFor(type)
                    .setCoercion(CoercionInputShape.String, CoercionAction.Fail)
                    .setCoercion(CoercionInputShape.EmptyString, CoercionAction.Fail)
                    .setCoercion(CoercionInputShape.Float, CoercionAction.Fail);
              }
              mapper
                  .coercionConfigFor(LogicalType.Boolean)
                  .setCoercion(CoercionInputShape.Integer, CoercionAction.Fail);
            });
  }
}

package com.hommey.backend.common;

import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.util.Map;
import org.springframework.stereotype.Component;

@Component
public class JsonCodec {
  private final ObjectMapper mapper;

  public JsonCodec(ObjectMapper mapper) {
    this.mapper = mapper;
  }

  public String write(Object value) {
    try {
      return mapper.writeValueAsString(value);
    } catch (Exception ex) {
      throw new IllegalArgumentException("Invalid JSON", ex);
    }
  }

  public Map<String, Object> object(Object value) {
    if (value == null) return new java.util.LinkedHashMap<>();
    try {
      return mapper.readValue(
          value.toString(), new TypeReference<java.util.LinkedHashMap<String, Object>>() {});
    } catch (Exception ex) {
      throw new IllegalArgumentException("Invalid JSON object", ex);
    }
  }

  public Object read(Object value) {
    try {
      return value == null ? null : mapper.readValue(value.toString(), Object.class);
    } catch (Exception ex) {
      throw new IllegalArgumentException("Invalid JSON", ex);
    }
  }
}

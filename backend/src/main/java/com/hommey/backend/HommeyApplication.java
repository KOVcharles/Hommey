package com.hommey.backend;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;

@SpringBootApplication
@ConfigurationPropertiesScan
public class HommeyApplication {
  public static void main(String[] args) {
    SpringApplication.run(HommeyApplication.class, args);
  }
}

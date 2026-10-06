package com.hommey.backend.security;

import cn.dev33.satoken.dao.SaTokenDao;
import cn.dev33.satoken.dao.SaTokenDaoForRedisTemplate;
import cn.dev33.satoken.interceptor.SaInterceptor;
import cn.dev33.satoken.stp.StpUtil;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Primary;
import org.springframework.web.servlet.config.annotation.InterceptorRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

@Configuration
public class SaTokenConfiguration implements WebMvcConfigurer {
  @Override
  public void addInterceptors(InterceptorRegistry registry) {
    registry
        .addInterceptor(new SaInterceptor(handler -> StpUtil.checkLogin()))
        .addPathPatterns("/api/**", "/auth/logout", "/auth/logout-all");
  }

  @Bean
  @Primary
  SaTokenDao saTokenDao() {
    return new SaTokenDaoForRedisTemplate() {
      @Override
      public String wrapKey(String key) {
        return "hommey:auth:session:" + key;
      }
    };
  }
}

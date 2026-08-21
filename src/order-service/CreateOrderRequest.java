package com.example.order.dto;

import lombok.Data;
import java.util.List;

@Data
public class CreateOrderRequest {
    private Long userId;
    private Long tenantId;
    private List<Item> items;
    private Long couponId;
    private Long addressId;

    @Data
    public static class Item {
        private String skuId;
        private Integer count;
        private Long price;
    }
}
